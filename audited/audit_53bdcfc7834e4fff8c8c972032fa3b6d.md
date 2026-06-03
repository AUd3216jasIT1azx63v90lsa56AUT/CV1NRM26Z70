Audit Report

## Title
Authorized Signer Can Unilaterally Consume Authorizer's Nonce via Self-Deauthorization - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` increments `nonce[authorization.authorizer]` unconditionally at line 26, before the signer check at lines 33–36. An authorized operator (Bob) can craft a self-deauthorization struct signed with his own key, pass the signer check via `IMidnight.isAuthorized(alice, bob)` (which is still `true` at check time due to TOCTOU), and permanently advance Alice's nonce — invalidating any pre-signed `Authorization` Alice has distributed at that nonce to third parties such as Charlie.

## Finding Description

**Root cause — `src/periphery/EcrecoverAuthorizer.sol` lines 24–48:**

```solidity
// line 26 — nonce consumed unconditionally before signer check
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());

// lines 33–36 — signer check: authorizer OR any address currently authorized by authorizer in Midnight
require(
    signer == authorization.authorizer
        || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);

// lines 46–47 — executes whatever the struct says; no guard on authorized == signer with isAuthorized = false
IMidnight(MIDNIGHT).setIsAuthorized(
    authorization.authorized, authorization.isAuthorized, authorization.authorizer
);
```

Three independent flaws combine:
1. **Unconditional nonce increment** at line 26 — the nonce advances regardless of what the authorization struct contains.
2. **TOCTOU** — `IMidnight.isAuthorized(alice, bob)` at line 34 is read *before* the deauthorization executes at line 47, so the check passes even though the transaction's net effect is to revoke Bob's authorization.
3. **No guard on self-deauthorization** — nothing prevents `authorization.authorized == signer` combined with `authorization.isAuthorized = false`.

**Preconditions (all normal operational states):**
1. Alice calls `midnight.setIsAuthorized(ecrecoverAuthorizer, true, alice)` — required to use the gasless flow.
2. Alice calls `midnight.setIsAuthorized(bob, true, alice)` — standard delegation.
3. Alice off-chain signs `Authorization{authorizer:alice, authorized:charlie, isAuthorized:true, nonce:0, deadline:T}` and distributes it to Charlie.

**Exploit flow:**
1. Bob constructs `Authorization{authorizer:alice, authorized:bob, isAuthorized:false, nonce:0, deadline:T2}` and signs it with his own private key.
2. Bob calls `ecrecoverAuthorizer.setIsAuthorized(auth, sig)`.
3. Line 26: `0 == nonce[alice]++` → passes; `nonce[alice]` becomes `1`.
4. Lines 33–36: `ecrecover` returns Bob; `IMidnight.isAuthorized(alice, bob)` is `true` at this point → passes.
5. Lines 46–47: `midnight.setIsAuthorized(bob, false, alice)` executes with `msg.sender = ecrecoverAuthorizer` (which Alice has authorized) → Bob is deauthorized from Midnight.
6. Charlie submits Alice's pre-signed message (nonce=0): `require(0 == nonce[alice])` → `0 == 1` → **reverts with `InvalidNonce`**.

**Why existing checks fail:**
The signer check at lines 33–36 is satisfied because `isAuthorized[alice][bob]` is read before the deauthorization executes on line 47. The nonce increment at line 26 is unconditional and permanent once the transaction succeeds. No guard exists on `authorization.authorized == signer` with `authorization.isAuthorized = false`.

## Impact Explanation
Alice's nonce in `EcrecoverAuthorizer` advances from N to N+1 without her consent. Any pre-signed `Authorization` she has distributed at nonce N — to Charlie, a relayer, or a smart contract — is permanently invalidated and cannot be submitted. Alice must re-sign at nonce N+1 and redistribute. If Alice is offline, unavailable, or the signed message was embedded in a time-sensitive workflow, the authorization is lost for that window. This constitutes unauthorized state corruption (nonce manipulation) and service disruption of the gasless authorization flow. The attack is repeatable each time Alice re-authorizes Bob.

## Likelihood Explanation
All three preconditions are normal operational states. Authorizing `EcrecoverAuthorizer` in Midnight is required to use the gasless flow at all. Authorizing Bob is a standard delegation. Having a pending pre-signed authorization is the core use case of `EcrecoverAuthorizer`. Bob requires no funds, no special protocol role beyond being authorized by Alice, and no external oracle. The attack is a single transaction executable by any authorized operator. Bob is a user-level operator, not a protocol-level privileged address, so this is not excluded by the SECURITY.md trusted-operator exclusion.

## Recommendation
Apply one or more of the following mitigations:

1. **Move the nonce increment after the signer check** — only advance the nonce once the signer is verified, so a failed authorization does not consume the nonce.
2. **Guard against self-deauthorization** — add `require(authorization.authorized != signer || authorization.isAuthorized, CannotSelfDeauthorize())` to prevent an authorized signer from deauthorizing themselves via EcrecoverAuthorizer.
3. **Re-check authorization after execution** — after calling `midnight.setIsAuthorized`, verify that the signer is still authorized (or was the authorizer), closing the TOCTOU window.
4. **Restrict `authorization.authorizer` to `msg.sender` or the signer** — require that only the authorizer themselves (not a delegated signer) can submit authorizations through this contract, eliminating the delegation-abuse vector entirely.

## Proof of Concept

**Minimal Foundry test plan:**

```solidity
// Setup
address alice = makeAddr("alice");
address bob = makeAddr("bob");
address charlie = makeAddr("charlie");

// Alice authorizes ecrecoverAuthorizer and bob in Midnight
vm.prank(alice);
midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, alice);
vm.prank(alice);
midnight.setIsAuthorized(bob, true, alice);

// Alice pre-signs Authorization{authorizer:alice, authorized:charlie, isAuthorized:true, nonce:0}
// (off-chain, distributed to Charlie)
bytes memory aliceSig = signAuthorization(aliceKey, alice, charlie, true, 0, deadline);

// Bob crafts self-deauthorization and signs with his own key
Authorization memory bobAuth = Authorization({
    authorizer: alice, authorized: bob, isAuthorized: false, nonce: 0, deadline: deadline2
});
bytes memory bobSig = signAuthorization(bobKey, alice, bob, false, 0, deadline2);

// Bob submits — consumes nonce[alice] = 0 → 1
vm.prank(bob);
ecrecoverAuthorizer.setIsAuthorized(bobAuth, bobSig);

// Assert nonce[alice] == 1
assertEq(ecrecoverAuthorizer.nonce(alice), 1);

// Charlie tries to submit Alice's pre-signed message — reverts
Authorization memory charlieAuth = Authorization({
    authorizer: alice, authorized: charlie, isAuthorized: true, nonce: 0, deadline: deadline
});
vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
vm.prank(charlie);
ecrecoverAuthorizer.setIsAuthorized(charlieAuth, aliceSig);
```