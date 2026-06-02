Audit Report

## Title
Authorized Midnight Delegate Can Consume Authorizer's EcrecoverAuthorizer Nonce, Permanently Invalidating Pre-Signed Authorization Messages - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` increments `nonce[authorization.authorizer]` unconditionally at line 26 before the signer is recovered or validated. The subsequent signer check at lines 33–36 explicitly accepts any address for which `IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer)` returns true. Any Midnight-level delegate of Alice can therefore construct and submit an `Authorization` struct with `authorizer=Alice` and Alice's current nonce, consuming it and permanently invalidating all of Alice's pre-signed messages carrying that nonce.

## Finding Description

**Root cause** — `src/periphery/EcrecoverAuthorizer.sol`, lines 24–48:

```solidity
// Line 26: nonce consumed before signer is known
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());

// Lines 31–36: signer check evaluated after nonce is already incremented
address signer = ecrecover(digest, signature.v, signature.r, signature.s);
require(signer != address(0), InvalidSignature());
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

The post-increment `nonce[authorization.authorizer]++` at line 26 permanently advances the nonce regardless of who the signer turns out to be. The signer check at lines 33–36 explicitly permits any address `s` for which `isAuthorized[authorization.authorizer][s]` is `true` in the core `Midnight` contract — i.e., any user-level delegate of the authorizer.

**Exploit flow:**

1. Alice calls `midnight.setIsAuthorized(Bob, true, Alice)` — `isAuthorized[Alice][Bob] = true`.
2. Alice off-chain pre-signs `Authorization(authorizer=Alice, authorized=Dave, isAuthorized=true, nonce=0, deadline=T)` and distributes or holds it for later submission.
3. Bob constructs `Authorization(authorizer=Alice, authorized=Charlie, isAuthorized=true, nonce=0, deadline=T')` and signs it with his own key.
4. Bob calls `ecrecoverAuthorizer.setIsAuthorized(auth_bob, sig_bob)`.
5. Line 26: `nonce[Alice]` is 0; `authorization.nonce (0) == nonce[Alice] (0)` passes; nonce advances to 1.
6. Lines 33–36: `signer == Bob`; `Midnight.isAuthorized(Alice, Bob) == true`; check passes.
7. Alice's pre-signed message `(authorizer=Alice, nonce=0)` now reverts with `InvalidNonce()` on any future submission.

**Why existing checks fail:**

- The nonce check at line 26 only verifies the value matches and then increments — it does not restrict *who* may consume it.
- The signer check at lines 33–36 is evaluated *after* the nonce is already consumed; even if reordered, it still explicitly permits authorized delegates.
- `Midnight.sol` line 192 (`mapping(address authorizer => mapping(address authorized => bool)) public isAuthorized`) and line 731–734 (`setIsAuthorized`) confirm the delegation mapping is flat and grants full delegation rights, including to peripheral contracts that re-use it.
- The `Midnight.sol` NatSpec at lines 101–110 acknowledges that "other contracts might re-use Midnight's authorization mapping too (e.g ratifiers and authorizers)" and that "authorized accounts can authorize other accounts on behalf of the user," but does not document nonce-consumption in `EcrecoverAuthorizer` as an accepted risk.

## Impact Explanation
Any address Alice has authorized in Midnight can consume Alice's current `EcrecoverAuthorizer` nonce by signing and submitting an `Authorization` struct with `authorizer=Alice` and the current nonce value. This permanently invalidates all of Alice's pre-signed authorization messages carrying that nonce. Alice must re-sign with the new nonce, but the attacker can immediately repeat the attack, creating a sustained, repeatable DoS on Alice's ability to use gasless/pre-signed delegations via `EcrecoverAuthorizer`. The impact is a permanent, repeatable freeze of a core protocol feature for any user with at least one Midnight delegate.

## Likelihood Explanation
**Preconditions:** Alice must have authorized Bob in Midnight — a normal, expected protocol operation with no special privileges required. Bob must act adversarially or be compromised. No funds are at risk directly, but the DoS is persistent and requires active remediation (revoking Bob's Midnight authorization). The attack is repeatable: after each nonce consumption, Bob can immediately consume the next nonce. No special on-chain privileges, admin keys, or leaked credentials are required.

## Recommendation
Move the nonce increment to *after* the signer check, and restrict nonce consumption to the authorizer only (not delegates):

```solidity
address signer = ecrecover(digest, signature.v, signature.r, signature.s);
require(signer != address(0), InvalidSignature());
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```

More robustly, restrict `setIsAuthorized` so that only `authorization.authorizer` themselves (i.e., `signer == authorization.authorizer`) may consume the nonce, and require delegates to use a separate, delegate-specific nonce or a direct on-chain call. Alternatively, use a per-signer nonce (`nonce[signer]`) rather than `nonce[authorization.authorizer]` so that Bob's submission only consumes Bob's nonce, not Alice's.

## Proof of Concept

**Minimal Foundry test:**

```solidity
function test_delegateConsumesAuthorizerNonce() public {
    // Alice authorizes Bob in Midnight
    vm.prank(alice);
    midnight.setIsAuthorized(bob, true, alice);

    // Alice pre-signs Authorization(authorizer=alice, authorized=dave, nonce=0)
    // (held off-chain, not submitted yet)

    // Bob constructs and signs Authorization(authorizer=alice, authorized=charlie, nonce=0)
    Authorization memory auth = Authorization({
        authorizer: alice,
        authorized: charlie,
        isAuthorized: true,
        nonce: 0,
        deadline: block.timestamp + 1 days
    });
    bytes32 digest = ecrecoverAuthorizer.hashAuthorization(auth);
    (uint8 v, bytes32 r, bytes32 s) = vm.sign(bobKey, digest);

    // Bob submits — consumes alice's nonce 0
    vm.prank(bob);
    ecrecoverAuthorizer.setIsAuthorized(auth, Signature(v, r, s));

    // Alice's pre-signed message now reverts
    Authorization memory aliceAuth = Authorization({
        authorizer: alice,
        authorized: dave,
        isAuthorized: true,
        nonce: 0,  // already consumed
        deadline: block.timestamp + 1 days
    });
    (uint8 av, bytes32 ar, bytes32 as_) = vm.sign(aliceKey, ecrecoverAuthorizer.hashAuthorization(aliceAuth));
    vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
    ecrecoverAuthorizer.setIsAuthorized(aliceAuth, Signature(av, ar, as_));
}
```