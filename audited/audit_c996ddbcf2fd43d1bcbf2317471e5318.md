Audit Report

## Title
Permissionless `setIsAuthorized` enables front-running of revocation with a stale same-nonce grant — (`src/periphery/EcrecoverAuthorizer.sol`)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` has no `msg.sender` restriction, allowing any party holding a previously-signed but unsubmitted grant `{nonce:N, isAuthorized:true}` to front-run a revocation `{nonce:N, isAuthorized:false}` broadcast by the authorizer. Because both messages are valid for nonce N and whichever lands first permanently consumes it, the revocation reverts with `InvalidNonce`, leaving the authorized party's access intact against the authorizer's explicit intent.

## Finding Description

**Root cause**

`setIsAuthorized` at `src/periphery/EcrecoverAuthorizer.sol` lines 24–26:

```solidity
function setIsAuthorized(Authorization memory authorization, Signature calldata signature) external {
    require(block.timestamp <= authorization.deadline, Expired());
    require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```

There is no `msg.sender` check — the permissionless design is confirmed by `testEcrecoverAuthorizerPermissionless` in `test/SetIsAuthorizedWithSigTest.sol` lines 74–86. The nonce check enforces sequential ordering only. The `isAuthorized` boolean is part of the EIP-712 struct hash (`Authorization` struct at `src/periphery/interfaces/IEcrecoverAuthorizer.sol` lines 11–17), so `{nonce:N, isAuthorized:true}` and `{nonce:N, isAuthorized:false}` produce different digests and require different signatures — but **both are valid for nonce N**. Whichever is submitted first consumes nonce N; the other permanently fails.

**Exploit flow**

1. Authorizer A signs `grant = {authorizer:A, authorized:B, isAuthorized:true, nonce:N, deadline:T}` and hands it to B for later submission. `nonce[A]` remains `N`.
2. A decides to revoke and signs `revoke = {authorizer:A, authorized:B, isAuthorized:false, nonce:N, deadline:T'}` and broadcasts it.
3. B observes the mempool, front-runs with `grant` (nonce N, `isAuthorized:true`).
4. `grant` lands first: `nonce[A]` increments to `N+1`; `midnight.isAuthorized(A, B)` becomes `true`.
5. A's `revoke` executes: `authorization.nonce (N) != nonce[A] (N+1)` → reverts `InvalidNonce`.

**Why existing checks fail**

- The deadline check (`block.timestamp <= authorization.deadline`) does not help: the grant's deadline is still in the future by precondition.
- The nonce check enforces ordering but does not prevent a competing same-nonce message from winning the race.
- There is no `msg.sender == authorization.authorizer` guard restricting who may submit a signature.

## Impact Explanation
B retains `midnight.isAuthorized(A, B) == true` after A explicitly signed and broadcast a revocation. Any protocol action gated on A's authorization of B (e.g., B acting as a delegate or ratifier on A's positions via `Midnight.setIsAuthorized`, `take`, `setConsumed`, etc.) remains available to B indefinitely. The attack is repeatable: each time A attempts revocation via signature, B can front-run with the next unsubmitted grant for that nonce. This constitutes an unauthorized state change / privilege retention — a concrete in-scope impact per RESEARCHER.md ("Authorization bypass leading to privileged action as unprivileged user").

## Likelihood Explanation
**Preconditions:**
1. A signed a grant with a future deadline and shared it with B without submitting it — the standard off-chain delegation flow the contract is designed for.
2. A later wants to revoke and signs a revocation with the same nonce (the only available nonce since the grant was never submitted).
3. B is the natural attacker: they already hold the signed grant and have direct economic incentive to remain authorized.

This scenario arises naturally whenever a user signs a delegation off-chain and later changes their mind before the grant is submitted. It is repeatable for every subsequent revocation attempt as long as B holds further unsubmitted grants for higher nonces.

## Recommendation
**Option 1 (minimal):** Add a `nonce invalidation` function that only the authorizer can call directly to skip nonces, invalidating all pending signed grants:
```solidity
function invalidateNonce(address authorizer) external {
    require(msg.sender == authorizer, Unauthorized());
    nonce[authorizer]++;
}
```
This allows A to burn nonce N on-chain before B can use the grant, then submit the revocation at nonce N+1.

**Option 2:** Restrict submission to `msg.sender == authorization.authorizer` only, removing the permissionless relay property but eliminating the race entirely.

**Option 3 (immediate workaround):** Document that revocation should be performed by calling `Midnight.setIsAuthorized(B, false, A)` directly (which requires `msg.sender == A`), bypassing `EcrecoverAuthorizer` entirely and avoiding the front-running window.

## Proof of Concept
Extend `test/SetIsAuthorizedWithSigTest.sol`:

```solidity
function testFrontRunRevocation() public {
    // Setup: authorize ecrecoverAuthorizer
    vm.prank(borrower);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, borrower);

    // Step 1: borrower signs a grant (nonce 0) but does NOT submit it
    Authorization memory grant = makeAuthorization(borrower, lender, true);
    Signature memory grantSig = signAuthorization(grant, borrower);

    // Step 2: borrower signs a revocation (same nonce 0) and "broadcasts" it
    Authorization memory revoke = makeAuthorization(borrower, lender, false);
    Signature memory revokeSig = signAuthorization(revoke, borrower);

    // Step 3: lender (attacker) front-runs with the grant
    vm.prank(lender);
    ecrecoverAuthorizer.setIsAuthorized(grant, grantSig);
    assertEq(midnight.isAuthorized(borrower, lender), true);
    assertEq(ecrecoverAuthorizer.nonce(borrower), 1);

    // Step 4: borrower's revocation now fails — nonce consumed
    vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
    ecrecoverAuthorizer.setIsAuthorized(revoke, revokeSig);

    // lender retains authorization despite borrower's explicit revocation attempt
    assertEq(midnight.isAuthorized(borrower, lender), true);
}
```