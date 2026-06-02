Audit Report

## Title
Authorized Midnight Agent Can Grief Victim's Pending Pre-Signed Authorization by Consuming the Current Nonce with a No-Op Re-Authorization - (`src/periphery/EcrecoverAuthorizer.sol`)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` permits any address for which `IMidnight.isAuthorized(authorizer, signer)` returns `true` to sign and submit an `Authorization` struct on behalf of the authorizer. Because the nonce at line 26 is incremented unconditionally on every successful execution and there is no guard requiring the resulting `setIsAuthorized` call to change state, an attacker who is already an authorized Midnight agent of the victim can construct and submit a no-op re-authorization (e.g., re-authorizing an already-authorized address) to burn the victim's current nonce and permanently invalidate any pending pre-signed authorization the victim has broadcast off-chain. The attack is cheap, repeatable, and requires no privileges beyond being an existing authorized agent.

## Finding Description

**Root cause:** The signer check at line 34 of `src/periphery/EcrecoverAuthorizer.sol` accepts two classes of signer: the authorizer themselves, or any address the authorizer has previously authorized on Midnight. The nonce at line 26 is incremented unconditionally on every successful call. There is no idempotency guard preventing a no-op re-authorization from consuming the nonce.

**Exact code path:**

```
Line 26: require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
Line 34: signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer)
Line 47: IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
``` [1](#0-0) 

**Attacker-controlled inputs:**
- `authorization.authorizer` = victim `V`
- `authorization.authorized` = `address(ecrecoverAuthorizer)` (already authorized → no-op)
- `authorization.isAuthorized` = `true`
- `authorization.nonce` = `N` (current `nonce[V]`)
- `authorization.deadline` = any future timestamp
- `signature` = attacker `A`'s own ECDSA signature over the above struct

**Exploit flow:**
1. Precondition: `midnight.isAuthorized(V, A) == true` (attacker is an authorized agent of victim).
2. Precondition: `midnight.isAuthorized(V, address(ecrecoverAuthorizer)) == true` (EcrecoverAuthorizer already authorized by victim — required for `EcrecoverAuthorizer` to call `midnight.setIsAuthorized` on behalf of `V`).
3. Victim `V` broadcasts a pre-signed `Authorization` with `nonce=N` off-chain (e.g., to a relayer or mempool).
4. Attacker `A` constructs `Authorization(authorizer=V, authorized=ecrecoverAuthorizer, isAuthorized=true, nonce=N, deadline=future)` and signs it with their own key.
5. Attacker calls `ecrecoverAuthorizer.setIsAuthorized(auth, sig)`.
6. Line 25: deadline check passes.
7. Line 26: `N == nonce[V]` passes; `nonce[V]` incremented to `N+1`.
8. Lines 28–31: digest computed; `ecrecover` returns `A`.
9. Line 34: `IMidnight(MIDNIGHT).isAuthorized(V, A) == true` → passes.
10. Line 47: `midnight.setIsAuthorized(ecrecoverAuthorizer, true, V)` → no-op (already authorized).
11. Victim's pending pre-signed authorization (nonce `N`) now reverts with `InvalidNonce` when submitted.

**Why existing checks fail:**
- The `Unauthorized` check (line 34) is designed to allow agents to act on behalf of the authorizer, but it does not distinguish between state-changing and no-op re-authorizations.
- There is no idempotency guard: `midnight.setIsAuthorized` at line 47 writes the same value unconditionally, and the nonce has already been consumed. [2](#0-1) 

- The Certora spec `EcrecoverAuthorizer.spec` rule `effects` (line 19) only asserts that the nonce increments on success — it does not rule out the case where a Midnight-authorized agent (not the authorizer) triggers that increment. [3](#0-2) 

## Impact Explanation
Any pending pre-signed `Authorization` the victim has created and shared off-chain (e.g., with a relayer, keeper, or counterparty) is permanently invalidated at the cost of a single transaction by any of the victim's existing Midnight-authorized agents. The victim must re-sign and re-broadcast. The attacker can repeat this indefinitely, making it impossible for the victim to use `EcrecoverAuthorizer` for off-chain authorization flows as long as the attacker remains an authorized agent. This constitutes a targeted, repeatable denial-of-service against the off-chain authorization flow of `EcrecoverAuthorizer`.

## Likelihood Explanation
**Preconditions:**
1. Attacker must hold `midnight.isAuthorized(victim, attacker) == true`. This is a realistic operational state: any protocol, keeper, or counterparty the victim has previously authorized satisfies it.
2. Victim must have a pending pre-signed authorization in flight (mempool, relayer queue, or shared off-chain).
3. `EcrecoverAuthorizer` must already be authorized by the victim (so the re-authorization is a no-op). If not, the attacker can still pick any other already-authorized address as `authorized`.

The attack is cheap (one transaction), repeatable, and requires no special privileges beyond being an existing authorized agent — a common operational state for any user interacting with protocols built on Midnight.

## Recommendation
Add an idempotency guard before incrementing the nonce: check whether the resulting `setIsAuthorized` call would actually change state, and revert if it is a no-op. Alternatively, restrict the signer check so that only `authorization.authorizer` themselves (not their agents) can sign an `Authorization` for `EcrecoverAuthorizer`. The latter is the cleaner fix, as the agent-signing path is the root cause of the vulnerability:

```solidity
require(signer == authorization.authorizer, Unauthorized());
```

If agent-signing is intentionally supported, add a state-change guard:

```solidity
bool currentState = IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, authorization.authorized);
require(currentState != authorization.isAuthorized, NoStateChange());
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```

Note: the nonce increment must come after the state-change check to prevent the guard from being bypassed by a race condition.

## Proof of Concept

```solidity
// Foundry test sketch
function testNonceGriefByAuthorizedAgent() public {
    address victim = borrower;
    address attacker = makeAddr("attacker");
    uint256 attackerKey = ...; // attacker's private key

    // Setup: victim authorizes EcrecoverAuthorizer and attacker on Midnight
    vm.prank(victim);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);
    vm.prank(victim);
    midnight.setIsAuthorized(attacker, true, victim);

    // Victim pre-signs an authorization (e.g., to authorize `lender`)
    uint256 currentNonce = ecrecoverAuthorizer.nonce(victim); // = 0
    Authorization memory victimAuth = Authorization({
        authorizer: victim,
        authorized: lender,
        isAuthorized: true,
        nonce: currentNonce,
        deadline: block.timestamp + 1 days
    });
    Signature memory victimSig = signAuthorization(victimAuth, victim);
    // victimAuth + victimSig broadcast off-chain...

    // Attacker constructs a no-op re-authorization signed with their own key
    Authorization memory attackAuth = Authorization({
        authorizer: victim,
        authorized: address(ecrecoverAuthorizer), // already authorized → no-op
        isAuthorized: true,
        nonce: currentNonce, // same nonce N
        deadline: block.timestamp + 1 days
    });
    Signature memory attackSig = signAuthorization(attackAuth, attacker);

    // Attacker submits first
    ecrecoverAuthorizer.setIsAuthorized(attackAuth, attackSig);
    assertEq(ecrecoverAuthorizer.nonce(victim), 1); // nonce consumed

    // Victim's pre-signed authorization now reverts
    vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
    ecrecoverAuthorizer.setIsAuthorized(victimAuth, victimSig);
}
```

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

**File:** certora/specs/EcrecoverAuthorizer.spec (L12-21)
```text
rule effects(env e, EcrecoverAuthorizer.Authorization authorization, EcrecoverAuthorizer.Signature signature, address other) {
    require other != authorization.authorizer;
    uint256 nonceBefore = nonce(authorization.authorizer);
    uint256 otherNonceBefore = nonce(other);

    setIsAuthorized(e, authorization, signature);

    assert nonce(authorization.authorizer) == nonceBefore + 1;
    assert nonce(other) == otherNonceBefore;
}
```
