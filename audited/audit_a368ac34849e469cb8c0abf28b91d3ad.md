### Title
Authorized Operator Can Consume Authorizer's EcrecoverAuthorizer Nonce to Indefinitely Block Signature-Based Revocation - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized()` permits any address that is already authorized by the authorizer in Midnight to sign and submit an `Authorization` struct on the authorizer's behalf, including one that re-affirms the signer's own authorization. Because the nonce belongs to the authorizer and increments on every successful call, an authorized attacker can consume the authorizer's next nonce at will, invalidating any pending EcrecoverAuthorizer-based revocation the victim has signed. The victim's only unblockable escape is the direct `Midnight.setIsAuthorized()` call, which bypasses EcrecoverAuthorizer entirely.

### Finding Description

**Exact code path:**

`EcrecoverAuthorizer.setIsAuthorized()` at [1](#0-0)  performs two independent checks before accepting a submission:

1. **Nonce check** (line 26): `authorization.nonce == nonce[authorization.authorizer]++` — the nonce belongs to `authorization.authorizer` (the victim), not the signer.
2. **Signer check** (lines 33–36): `signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer)` — any address that Midnight considers authorized by the authorizer is a valid signer.

There is **no check** that prevents `signer == authorization.authorized` (i.e., the signer signing a message that re-affirms their own authorization). The final call at line 47 is:

```solidity
IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
``` [2](#0-1) 

**Exploit flow:**

Preconditions:
- `midnight.isAuthorized(victim, EcrecoverAuthorizer) == true`
- `midnight.isAuthorized(victim, attacker) == true`

Step 1: Attacker constructs `Authorization{authorizer: victim, authorized: attacker, isAuthorized: true, nonce: nonce[victim], deadline: type(uint256).max}` and signs it with **attacker's own key**.

Step 2: Attacker calls `ecrecoverAuthorizer.setIsAuthorized(auth, sig)`.

Step 3: Line 25 passes (`block.timestamp <= MAX_UINT256`). Line 26 passes and increments `nonce[victim]`. Lines 33–36 pass because `isAuthorized[victim][attacker] == true`. Line 47 calls `midnight.setIsAuthorized(attacker, true, victim)` — a no-op re-affirmation.

Step 4: Victim signs `Authorization{..., isAuthorized: false, nonce: N}` to revoke. Attacker front-runs with `Authorization{..., isAuthorized: true, nonce: N}` signed by attacker. Victim's transaction reverts with `InvalidNonce`. Victim must re-sign with nonce N+1. Attacker front-runs again. This loop is unbounded.

**Why existing checks fail:**

The nonce check at line 26 is the only sequencing guard, but it protects against *replay*, not against a currently-authorized operator consuming the slot. The signer check at lines 33–36 intentionally allows authorized operators to act on behalf of the authorizer — but it does not restrict *self-serving* authorizations where `signer == authorized`. [3](#0-2) 

The direct revocation path in `Midnight.setIsAuthorized()` is unblockable: [4](#0-3)  — but this is a separate mechanism, not a check inside `EcrecoverAuthorizer` that stops the attack.

### Impact Explanation
An attacker who is already an authorized operator of the victim can prevent the victim from ever successfully revoking them *via EcrecoverAuthorizer*. Every off-chain-signed revocation the victim submits can be front-run by the attacker consuming the same nonce with a self-re-affirmation. The victim's EcrecoverAuthorizer-based revocation is indefinitely blocked for as long as the attacker is willing to pay gas. The victim retains the ability to revoke via a direct `Midnight.setIsAuthorized()` call, so the broader protocol authorization invariant is not broken — but the EcrecoverAuthorizer-specific revocation guarantee is violated.

### Likelihood Explanation
Preconditions are reachable in normal protocol use: a user authorizes an operator (e.g., a keeper or relayer) via EcrecoverAuthorizer, then later wants to revoke. The attacker only needs to monitor the mempool and submit a competing transaction with higher gas. The attack is repeatable indefinitely at low cost (one transaction per victim revocation attempt). No special privileges, oracle manipulation, or user mistakes are required beyond the initial authorization.

### Recommendation
Add a check in `setIsAuthorized` that, when the signer is not the authorizer themselves, prevents the signer from signing an authorization where `authorization.authorized == signer` (self-serving re-affirmation). Concretely, after the signer check, add:

```solidity
if (signer != authorization.authorizer) {
    require(signer != authorization.authorized, SelfServingOperatorForbidden());
}
```

This preserves the ability of authorized operators to manage *other* addresses on behalf of the authorizer while removing the nonce-griefing vector.

### Proof of Concept

```solidity
// Foundry stateful fuzz test
function testAttackerBlocksRevocationViaEcrecover(uint8 rounds) public {
    rounds = uint8(bound(rounds, 1, 20));

    // Setup: victim authorizes EcrecoverAuthorizer and attacker
    vm.prank(victim);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);
    vm.prank(victim);
    midnight.setIsAuthorized(attacker, true, victim);

    for (uint8 i = 0; i < rounds; i++) {
        uint256 currentNonce = ecrecoverAuthorizer.nonce(victim);

        // Victim signs revocation with current nonce
        Authorization memory revokeAuth = Authorization({
            authorizer: victim, authorized: attacker,
            isAuthorized: false, nonce: currentNonce, deadline: type(uint256).max
        });
        Signature memory victimSig = signAuthorization(revokeAuth, victim);

        // Attacker front-runs: re-affirm self with same nonce
        Authorization memory reaffirmAuth = Authorization({
            authorizer: victim, authorized: attacker,
            isAuthorized: true, nonce: currentNonce, deadline: type(uint256).max
        });
        Signature memory attackerSig = signAuthorization(reaffirmAuth, attacker);

        // Attacker's tx lands first
        ecrecoverAuthorizer.setIsAuthorized(reaffirmAuth, attackerSig);

        // Victim's tx now reverts
        vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
        ecrecoverAuthorizer.setIsAuthorized(revokeAuth, victimSig);

        // Assert: attacker still authorized, nonce advanced
        assertTrue(midnight.isAuthorized(victim, attacker));
        assertEq(ecrecoverAuthorizer.nonce(victim), currentNonce + 1);
    }
}
```

Expected assertions: all `rounds` iterations pass; `midnight.isAuthorized(victim, attacker)` remains `true` throughout; victim's revocation never lands via EcrecoverAuthorizer.

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

**File:** src/periphery/EcrecoverAuthorizer.sol (L46-48)
```text
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
