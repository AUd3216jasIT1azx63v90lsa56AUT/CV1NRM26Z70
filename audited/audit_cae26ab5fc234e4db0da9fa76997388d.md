### Title
Authorized-party nonce front-run invalidates victim's pending EcrecoverAuthorizer authorization - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` allows any address that is currently authorized by the victim on Midnight to sign and submit an `Authorization` struct on the victim's behalf. Because the nonce is consumed atomically on line 26 before the signer identity is verified on lines 33–36, an attacker who holds a Midnight authorization from the victim can front-run the victim's pending `setIsAuthorized` call with a different `Authorization` payload carrying the same nonce, consuming that nonce and causing the victim's transaction to revert with `InvalidNonce`. The attacker's chosen authorization — targeting any `authorized` address and any `isAuthorized` value — takes effect instead.

### Finding Description
**Code path:**

`src/periphery/EcrecoverAuthorizer.sol`, `setIsAuthorized`, lines 24–47.

```solidity
// line 26 — nonce consumed BEFORE signer is checked
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
...
// lines 33-36 — any Midnight-authorized party is accepted as signer
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

**Root cause:** The nonce increment is committed on line 26 as a side-effect of the `require` passing. The subsequent signer check on lines 33–36 accepts not only the authorizer themselves but also any address for which `isAuthorized[authorizer][signer]` is true on Midnight. There is no binding between a nonce and a specific authorization payload from the authorizer's own intent; any authorized party can craft an arbitrary `Authorization` struct with the victim's current nonce and a valid signature from their own key.

**Exploit flow:**

1. Precondition: `isAuthorized[victim][attacker] == true` on Midnight (attacker was previously authorized by victim for any reason).
2. Victim signs `Authorization{authorizer=victim, authorized=X, isAuthorized=true, nonce=N, deadline=T}` and broadcasts to `EcrecoverAuthorizer.setIsAuthorized`.
3. Attacker observes the mempool and front-runs with `Authorization{authorizer=victim, authorized=attacker_addr, isAuthorized=true, nonce=N, deadline=T'}` signed by the attacker's own key.
4. Attacker's tx executes first: line 26 passes (N == N), nonce becomes N+1; lines 33–36 pass because `isAuthorized[victim][attacker]` is true; attacker's chosen authorization is written to Midnight.
5. Victim's tx executes: line 26 fails (N != N+1) → `InvalidNonce` revert.

**Why existing checks fail:** The `InvalidNonce` check only prevents replay of an already-consumed nonce; it does not prevent a different authorized party from consuming the nonce first with a different payload. The `Unauthorized` check is the intended delegation feature but is the enabler of the attack.

### Impact Explanation
The victim's pending authorization is permanently invalidated for nonce N, forcing re-signing. The attacker's chosen authorization — which can target any `authorized` address with any `isAuthorized` value — takes effect on Midnight. The attacker can repeat this for every subsequent nonce as long as they remain authorized, making the `EcrecoverAuthorizer` path permanently unusable for the victim without first revoking the attacker's Midnight authorization via the direct `Midnight.setIsAuthorized` call.

### Likelihood Explanation
The precondition — attacker holds a Midnight authorization from the victim — is reachable via any prior `setIsAuthorized` call (direct or via `EcrecoverAuthorizer`). Ratifiers, liquidators, and other protocol participants are commonly authorized. Front-running is straightforward on any chain with a public mempool. The attack is repeatable at zero cost beyond gas.

### Recommendation
Restrict nonce consumption to signatures from the authorizer themselves. Remove the delegated-signer path from `EcrecoverAuthorizer.setIsAuthorized`, or add a separate check that the signer must equal `authorization.authorizer`:

```solidity
require(signer == authorization.authorizer, Unauthorized());
```

If delegated signing is intentionally desired, bind the nonce to the specific delegated signer (e.g., include the signer's address in the signed struct) so that only the intended signer can consume a given nonce with a given payload.

### Proof of Concept
```solidity
// Foundry unit test
function testFrontRunNonce() public {
    // Setup: victim authorizes attacker on Midnight directly
    vm.prank(victim);
    midnight.setIsAuthorized(attacker, true, victim);

    // Victim prepares their intended authorization (nonce=0)
    Authorization memory victimAuth = Authorization({
        authorizer: victim,
        authorized: intendedAddress,
        isAuthorized: true,
        nonce: 0,
        deadline: block.timestamp + 1 days
    });
    Signature memory victimSig = signAuthorization(victimAuth, victim);

    // Attacker front-runs with a different Authorization at the same nonce=0
    Authorization memory attackerAuth = Authorization({
        authorizer: victim,
        authorized: attackerControlledAddress,
        isAuthorized: true,
        nonce: 0,
        deadline: block.timestamp + 1 days
    });
    Signature memory attackerSig = signAuthorization(attackerAuth, attacker);

    vm.prank(attacker);
    ecrecoverAuthorizer.setIsAuthorized(attackerAuth, attackerSig);

    // Assert attacker's authorization took effect
    assertEq(midnight.isAuthorized(victim, attackerControlledAddress), true);
    // Assert nonce advanced
    assertEq(ecrecoverAuthorizer.nonce(victim), 1);

    // Victim's tx now reverts with InvalidNonce
    vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
    ecrecoverAuthorizer.setIsAuthorized(victimAuth, victimSig);

    // Victim's intended authorization never took effect
    assertEq(midnight.isAuthorized(victim, intendedAddress), false);
}
```

Expected: attacker's `setIsAuthorized` succeeds, victim's reverts with `InvalidNonce`, `isAuthorized[victim][intendedAddress]` remains false, `isAuthorized[victim][attackerControlledAddress]` is true. [1](#0-0) [2](#0-1)

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L24-36)
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
```

**File:** src/periphery/interfaces/IEcrecoverAuthorizer.sol (L11-17)
```text
struct Authorization {
    address authorizer;
    address authorized;
    bool isAuthorized;
    uint256 nonce;
    uint256 deadline;
}
```
