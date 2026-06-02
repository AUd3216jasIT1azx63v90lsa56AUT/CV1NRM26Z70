Audit Report

## Title
Authorized Operator Can Self-Revoke and Consume Victim's EcrecoverAuthorizer Nonce, Invalidating Pre-Signed Authorizations - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` permits any address holding a live Midnight authorization from `authorization.authorizer` to sign and submit an `Authorization` struct on the authorizer's behalf — including one that revokes the signer's own authorization. Because `nonce[authorization.authorizer]` is incremented at line 26 before the signer check at lines 33–36, `operatorA` can consume victim's nonce at value `N` while simultaneously self-revoking, permanently invalidating any pre-signed authorization victim has issued at that nonce.

## Finding Description

**Root cause:** `EcrecoverAuthorizer.setIsAuthorized` contains no guard preventing `authorization.authorized == signer` when `authorization.isAuthorized == false`. The signer validity check at lines 33–36 accepts any address satisfying `isAuthorized(authorization.authorizer, signer)` in Midnight — including `operatorA` itself.

**Exact code path:**

`EcrecoverAuthorizer.sol` line 26 increments the nonce before any signer check:
```solidity
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```

Lines 33–36 then check:
```solidity
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

Lines 46–47 execute the downstream call:
```solidity
IMidnight(MIDNIGHT)
    .setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
```

`Midnight.sol` line 732 checks:
```solidity
require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**Exploit flow:**

1. **Preconditions:** `isAuthorized[victim][operatorA] == true` and `isAuthorized[victim][EcrecoverAuthorizer] == true` in Midnight.
2. `operatorA` constructs `Authorization{authorizer=victim, authorized=operatorA, isAuthorized=false, nonce=N, deadline=...}` where `N` is victim's current EcrecoverAuthorizer nonce.
3. `operatorA` signs the EIP-712 digest and calls `EcrecoverAuthorizer.setIsAuthorized(auth, sig)`.
4. Line 26: `nonce[victim]` increments from `N` to `N+1` — nonce is consumed before signer validation.
5. Lines 33–36: `signer == authorization.authorizer` → `operatorA == victim` → FALSE; `isAuthorized(victim, operatorA)` → TRUE → check passes.
6. Lines 46–47: `midnight.setIsAuthorized(operatorA, false, victim)` is called.
7. `Midnight.setIsAuthorized` line 732: `isAuthorized[victim][EcrecoverAuthorizer]` → TRUE → check passes; sets `isAuthorized[victim][operatorA] = false`.

**Why existing checks fail:** The only authorization check in `EcrecoverAuthorizer` verifies that the signer is either the authorizer or someone the authorizer has authorized in Midnight. There is no check that `signer != authorization.authorized`, and no check preventing a revocation (`isAuthorized=false`) from being signed by the very address being revoked.

## Impact Explanation
`operatorA` can grief victim by consuming victim's EcrecoverAuthorizer nonce at any value `N`. If victim has pre-signed `Authorization{authorizer=victim, authorized=operatorB, isAuthorized=true, nonce=N}` and distributed it off-chain (e.g., to a relayer or counterparty for gasless UX), `operatorA` can front-run the submission by consuming nonce `N` first, permanently invalidating `operatorB`'s pre-signed authorization. Victim must re-sign at nonce `N+1`. This constitutes unauthorized state corruption of victim's EcrecoverAuthorizer nonce sequence and disruption of the primary use case of the contract.

## Likelihood Explanation
Preconditions are standard protocol usage: victim authorizes `EcrecoverAuthorizer` (required to use the sig-based flow at all) and authorizes `operatorA` (normal delegation). The attack requires `operatorA` to act maliciously or be compromised. It is a low-cost, single-transaction griefing action with no financial requirement. The scenario is most dangerous when victim has distributed pre-signed authorizations off-chain for gasless UX flows, which is the primary use case of `EcrecoverAuthorizer`. The attack is repeatable if victim re-authorizes `operatorA`.

## Recommendation
Add a guard in `EcrecoverAuthorizer.setIsAuthorized` that prevents a signer from revoking their own authorization:

```solidity
require(
    authorization.isAuthorized || signer != authorization.authorized,
    SelfRevocationForbidden()
);
```

This check should be placed after the signer is recovered (line 31) and before or alongside the existing authorization check (lines 33–36). Alternatively, restrict the signer to only be `authorization.authorizer` (removing the delegated-signer path entirely), or require that when `isAuthorized == false`, only the authorizer themselves can sign.

## Proof of Concept

**Minimal Foundry test outline:**

```solidity
// Setup
midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);  // victim authorizes EcrecoverAuthorizer
midnight.setIsAuthorized(operatorA, true, victim);                      // victim authorizes operatorA

// operatorA constructs self-revocation authorization at victim's current nonce N
Authorization memory auth = Authorization({
    authorizer: victim,
    authorized: operatorA,
    isAuthorized: false,
    nonce: ecrecoverAuthorizer.nonce(victim), // N
    deadline: block.timestamp + 1 hours
});

// operatorA signs and submits
bytes32 digest = ...; // EIP-712 digest
(uint8 v, bytes32 r, bytes32 s) = vm.sign(operatorAPrivKey, digest);
vm.prank(operatorA);
ecrecoverAuthorizer.setIsAuthorized(auth, Signature(v, r, s));

// Assert: nonce[victim] == N+1 (consumed)
assertEq(ecrecoverAuthorizer.nonce(victim), N + 1);
// Assert: operatorA is no longer authorized
assertFalse(midnight.isAuthorized(victim, operatorA));
// Assert: victim's pre-signed auth at nonce N for operatorB is now invalid (nonce consumed)
```