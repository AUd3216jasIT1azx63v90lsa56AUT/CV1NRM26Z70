The code confirms the claim. Let me verify the exact behavior:

- [1](#0-0)  — nonce is incremented unconditionally before any signer identity is established.
- [2](#0-1)  — signer check explicitly accepts any Midnight-level delegate.
- [3](#0-2)  — Certora spec only asserts nonce increments by 1 on success; it does not constrain that only the authorizer (not a delegate) may trigger that increment.

The exploit path is fully reachable with no privileged assumptions beyond a normal delegate relationship, which is an expected protocol operation per [4](#0-3)  and [5](#0-4) .

---

Audit Report

## Title
Authorized Midnight Delegate Can Consume Authorizer's EcrecoverAuthorizer Nonce, Permanently Invalidating Pre-Signed Authorization Messages - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
In `EcrecoverAuthorizer.setIsAuthorized`, the authorizer's nonce is incremented at line 26 before the signer is recovered or validated. The subsequent signer check at lines 33–36 explicitly accepts any address for which `IMidnight.isAuthorized(authorization.authorizer, signer)` returns true. Any Midnight-level delegate of Alice can therefore sign a fresh `Authorization` struct naming Alice as authorizer, consume Alice's current nonce, and permanently invalidate any pre-signed messages Alice holds for that nonce. The attack is repeatable at negligible cost for as long as the delegate relationship persists.

## Finding Description
**Root cause:** `src/periphery/EcrecoverAuthorizer.sol`, `setIsAuthorized` (lines 24–48):

```solidity
// Line 26: nonce consumed BEFORE signer is checked
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());

// Lines 31–36: signer check explicitly permits any Midnight delegate
address signer = ecrecover(digest, signature.v, signature.r, signature.s);
require(signer != address(0), InvalidSignature());
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

The nonce for `authorization.authorizer` is incremented unconditionally at line 26, before any signer identity is established. The signer check at lines 33–36 then accepts not only the authorizer but any address `signer` for which `Midnight.isAuthorized(authorization.authorizer, signer)` is true.

**Exploit flow:**
1. Alice calls `Midnight.setIsAuthorized(Bob, true, Alice)` — Bob is now a Midnight-level delegate of Alice (normal, expected operation).
2. Alice off-chain pre-signs `Authorization(authorizer=Alice, authorized=Dave, isAuthorized=true, nonce=0, deadline=T)`.
3. Bob constructs `Authorization(authorizer=Alice, authorized=Charlie, isAuthorized=true, nonce=0, deadline=T')` and signs it with his own key.
4. Bob calls `ecrecoverAuthorizer.setIsAuthorized(auth_bob, sig_bob)`.
5. Line 26: `nonce[Alice]` is incremented 0 → 1. The require passes because `authorization.nonce (0) == nonce[Alice] (0)`.
6. Lines 33–36: `signer == Bob`, and `Midnight.isAuthorized(Alice, Bob) == true`. The require passes.
7. Alice's pre-signed message `(authorizer=Alice, nonce=0)` now reverts with `InvalidNonce()` on any future submission attempt.
8. Bob can immediately repeat with nonce=1, 2, … as long as he remains a delegate.

**Why existing checks fail:**
- The nonce check at line 26 only verifies the value matches and increments it; it does not restrict *who* may consume it.
- The signer check at lines 33–36 is evaluated after the nonce is already consumed; even if evaluated first, it still explicitly permits delegates.
- The Certora `effects` rule in `certora/specs/EcrecoverAuthorizer.spec` (lines 12–21) only asserts that `nonce(authorization.authorizer)` increments by 1 on success and that other nonces are unchanged. It does not constrain that only the authorizer themselves (not a delegate) may trigger that increment, leaving this attack path unverified.

## Impact Explanation
Any address Alice has authorized in Midnight can consume Alice's current `EcrecoverAuthorizer` nonce by signing and submitting an `Authorization` struct with `authorizer=Alice` and the current nonce value. This permanently invalidates all of Alice's pre-signed authorization messages carrying that nonce. The DoS is persistent and repeatable: after each nonce consumption, the attacker can immediately consume the next nonce, rendering Alice's use of `EcrecoverAuthorizer` for pre-signed delegations completely non-functional as long as the attacker remains a delegate.

## Likelihood Explanation
Alice must have authorized Bob in Midnight — a normal, expected operation explicitly supported by the protocol's authorization model (`IMidnight.setIsAuthorized`). Bob must act adversarially or be compromised. No special privileges beyond being an existing Midnight-level delegate are required. The attack requires no capital, no flash loans, and no oracle manipulation. It is repeatable at negligible cost (gas only) and cannot be stopped without Alice revoking Bob's Midnight authorization.

## Recommendation
Restrict nonce consumption to the authorizer only. The signer check should require `signer == authorization.authorizer` exclusively — delegates should not be permitted to sign on behalf of the authorizer in this context. If delegate signing is intentionally desired, the nonce must be keyed per `(authorizer, signer)` pair rather than per `authorizer` alone, so a delegate's submission cannot invalidate the authorizer's own pre-signed messages. Additionally, the Certora spec should be extended with a rule asserting that only a valid signature from `authorization.authorizer` itself (not a delegate) can cause `nonce[authorization.authorizer]` to increment.

## Proof of Concept
```solidity
// Minimal Foundry test sketch
function test_delegateConsumesAuthorizerNonce() public {
    // Alice authorizes Bob in Midnight
    vm.prank(alice);
    midnight.setIsAuthorized(bob, true, alice);

    // Alice pre-signs Authorization(authorizer=alice, authorized=dave, nonce=0)
    // (held off-chain, not yet submitted)

    // Bob constructs and signs Authorization(authorizer=alice, authorized=charlie, nonce=0)
    Authorization memory auth = Authorization({
        authorizer: alice,
        authorized: charlie,
        isAuthorized: true,
        nonce: 0,
        deadline: block.timestamp + 1 hours
    });
    bytes32 digest = ecrecoverAuthorizer.hashAuthorization(auth);
    (uint8 v, bytes32 r, bytes32 s) = vm.sign(bobKey, digest);

    // Bob submits — consumes alice's nonce 0
    ecrecoverAuthorizer.setIsAuthorized(auth, Signature(v, r, s));
    assertEq(ecrecoverAuthorizer.nonce(alice), 1);

    // Alice's pre-signed message (nonce=0) now reverts
    vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
    ecrecoverAuthorizer.setIsAuthorized(alicePreSignedAuth, alicePreSignedSig);
}
```

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L26-26)
```text
        require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```

**File:** src/periphery/EcrecoverAuthorizer.sol (L33-36)
```text
        require(
            signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
            Unauthorized()
        );
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

**File:** src/interfaces/IMidnight.sol (L123-123)
```text
    function isAuthorized(address authorizer, address authorized) external view returns (bool);
```

**File:** src/interfaces/IMidnight.sol (L156-156)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external;
```
