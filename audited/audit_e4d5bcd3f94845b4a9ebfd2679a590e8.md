Audit Report

## Title
Authorized delegate can front-run and consume victim's EcrecoverAuthorizer nonce with arbitrary payload - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` allows any address that `IMidnight.isAuthorized(authorizer, signer)` returns `true` for to sign an `Authorization` struct with fully attacker-controlled `authorized` and `isAuthorized` fields on behalf of the authorizer. Because the nonce is incremented before the signer is verified, a Midnight-authorized delegate can front-run the authorizer's pending transaction, consume nonce N with an attacker-chosen payload, permanently invalidate the authorizer's signature, and substitute an attacker-controlled `authorized` address in the victim's Midnight `isAuthorized` mapping.

## Finding Description

**Root cause — two cooperating flaws:**

**Flaw 1 — Nonce incremented before signer verification** (`src/periphery/EcrecoverAuthorizer.sol`, line 26):
```solidity
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```
The nonce for `authorization.authorizer` is consumed unconditionally before the signature is checked. Any caller who can pass the signer check with a matching nonce will burn that nonce, regardless of the payload.

**Flaw 2 — Signer check does not bind the delegate to the authorizer's payload** (lines 33–36):
```solidity
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```
The check accepts any address that the authorizer has previously authorized in Midnight. It does not restrict which `authorized` or `isAuthorized` values the delegate may embed in the struct they sign. A delegate signs a completely different `Authorization` struct (different `authorized`, different `isAuthorized`) and the check still passes.

**Flaw 3 — State change uses fully attacker-controlled fields** (lines 46–47):
```solidity
IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
```
The `authorized` and `isAuthorized` fields come directly from the struct the attacker signed, not from the victim's intended struct.

**Exploit flow:**
1. Alice (authorizer) has previously called `midnight.setIsAuthorized(bob, true, alice)` — a routine operator authorization (as shown in `test/AuthorizationTest.sol` lines 100–103, 237–239).
2. Alice signs `Authorization{authorizer: alice, authorized: charlie, isAuthorized: true, nonce: N, deadline: T}` and broadcasts it.
3. Bob observes the pending transaction, constructs `Authorization{authorizer: alice, authorized: <attacker_address>, isAuthorized: true, nonce: N, deadline: <valid>}`, signs it with his own key, and front-runs Alice's transaction.
4. Line 26: nonce check passes (N == current nonce), nonce incremented to N+1.
5. Lines 33–36: signer check passes because `IMidnight.isAuthorized(alice, bob) == true`.
6. Lines 46–47: `IMidnight.setIsAuthorized(attacker_address, true, alice)` executes — Alice's mapping is modified with Bob's chosen values.
7. Alice's original transaction reverts with `InvalidNonce`.

**Why existing checks fail:**
- The nonce check does not prevent this because the nonce is consumed before the signer is verified.
- The signer check does not prevent this because it was designed to allow delegates to act on behalf of the authorizer, but it does not restrict which payload the delegate may sign — the EIP-712 digest is computed over the attacker's struct, not the victim's.
- There is no binding between the signer identity and the payload fields (`authorized`, `isAuthorized`).

## Impact Explanation

**DoS on EcrecoverAuthorizer flow**: The victim's signed-and-broadcast transaction is permanently invalidated. The attacker can repeat this for every subsequent nonce (N+1, N+2, …), permanently blocking the victim from using `EcrecoverAuthorizer` to manage their authorizations via signature. This is a concrete, repeatable service disruption.

**Unauthorized state substitution / privilege escalation**: The attacker substitutes an arbitrary `authorized` address in the victim's Midnight `isAuthorized` mapping. The EcrecoverAuthorizer is supposed to execute the authorizer's signed intent; instead it executes the delegate's substituted payload. This allows the attacker to grant or revoke authorization for any address in the victim's name, bypassing the victim's intent. This is a concrete, in-scope unauthorized state change.

## Likelihood Explanation

**Preconditions:**
1. The attacker has previously been authorized by the victim in Midnight for any purpose — a routine pattern for operators and bundlers, as demonstrated in `test/AuthorizationTest.sol` lines 100–103 and 237–239.
2. The victim has a pending `EcrecoverAuthorizer.setIsAuthorized` transaction visible in the mempool.

Both conditions are normal in protocol usage. The attack requires no privileged protocol access, no admin keys, and no leaked credentials — only a standard user-level authorization relationship. It is repeatable indefinitely at zero additional cost per nonce, and the attacker can sustain the DoS for as long as they remain authorized.

## Recommendation

**Option A (minimal, recommended):** Remove the delegate path from `EcrecoverAuthorizer`. The contract's purpose is to allow the authorizer to sign their own authorizations gaslessly; delegates who are already authorized in Midnight can call `midnight.setIsAuthorized` directly without a signature relay. Change line 33–36 to:
```solidity
require(signer == authorization.authorizer, Unauthorized());
```

**Option B:** If delegate signing is intentional, bind the nonce to the (authorizer, signer) pair rather than to the authorizer alone, so a delegate's front-run does not consume the authorizer's nonce:
```solidity
mapping(address authorizer => mapping(address signer => uint256)) public nonce;
```
This prevents nonce consumption cross-signer but does not prevent a delegate from signing arbitrary payloads.

**Option C:** Move the nonce increment after the signer check. This does not fix the payload substitution problem but prevents nonce consumption on failed signer checks.

The root fix is Option A: restrict `EcrecoverAuthorizer` to authorizer-only signatures.

## Proof of Concept

Minimal Foundry test (extend `SetIsAuthorizedWithSigTest.sol`):

```solidity
function testDelegateFrontRunNonce() public {
    // Alice authorizes Bob in Midnight (routine operator setup)
    vm.prank(alice);
    midnight.setIsAuthorized(bob, true, alice);

    // Alice also authorizes EcrecoverAuthorizer to act on her behalf
    vm.prank(alice);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, alice);

    // Alice signs her intended authorization: authorize charlie
    Authorization memory aliceAuth = Authorization({
        authorizer: alice,
        authorized: charlie,
        isAuthorized: true,
        nonce: 0,
        deadline: block.timestamp + 1 days
    });
    Signature memory aliceSig = signAuthorization(aliceAuth, alice);

    // Bob front-runs: constructs a different payload with same nonce, signs with his own key
    Authorization memory bobAuth = Authorization({
        authorizer: alice,
        authorized: attacker,
        isAuthorized: true,
        nonce: 0,
        deadline: block.timestamp + 1 days
    });
    Signature memory bobSig = signAuthorization(bobAuth, bob);

    // Bob submits first
    ecrecoverAuthorizer.setIsAuthorized(bobAuth, bobSig);

    // Attacker address is now authorized in Alice's mapping
    assertEq(midnight.isAuthorized(alice, attacker), true);
    // Charlie is NOT authorized (Alice's intent was ignored)
    assertEq(midnight.isAuthorized(alice, charlie), false);

    // Alice's transaction now reverts
    vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
    ecrecoverAuthorizer.setIsAuthorized(aliceAuth, aliceSig);
}
```