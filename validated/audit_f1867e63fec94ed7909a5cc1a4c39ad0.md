Audit Report

## Title
Authorized Delegate Can Burn Victim's Nonce via Self-Signed Authorization Struct — (`src/periphery/EcrecoverAuthorizer.sol`)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` increments `nonce[authorization.authorizer]` before verifying the signature, and the `Unauthorized` guard accepts any signer for whom `isAuthorized[authorizer][signer] == true`. An already-authorized party (ATTACKER) can construct and self-sign an `Authorization` struct naming the victim as `authorizer`, satisfy the delegation branch, and permanently consume the victim's current nonce — invalidating any pending off-chain authorization the victim has distributed. The attack is repeatable at gas cost only.

## Finding Description

**Exact code path** — `src/periphery/EcrecoverAuthorizer.sol` lines 24–48:

```solidity
function setIsAuthorized(Authorization memory authorization, Signature calldata signature) external {
    require(block.timestamp <= authorization.deadline, Expired());
    require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce()); // (1) nonce burned here

    bytes32 hashStruct = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, authorization));
    bytes32 domainSeparator = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(this)));
    bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSeparator, hashStruct));
    address signer = ecrecover(digest, signature.v, signature.r, signature.s);
    require(signer != address(0), InvalidSignature());
    require(
        signer == authorization.authorizer
            || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer), // (2) delegation branch
        Unauthorized()
    );
    IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
}
```

**Root cause** — The nonce at line 26 is keyed to `authorization.authorizer` (victim A), but the `Unauthorized` guard at lines 33–36 has a second branch that accepts any `signer` for whom `isAuthorized[A][signer] == true`. There is no requirement that the signer is A, nor that the `Authorization` struct was produced by A. ATTACKER can independently construct `Authorization{authorizer=A, authorized=ATTACKER, isAuthorized=true, nonce=N}` and sign it with ATTACKER's own private key. `ecrecover` returns ATTACKER's address; `isAuthorized[A][ATTACKER] == true` satisfies the delegation branch; `nonce[A]` advances N → N+1; and `IMidnight.setIsAuthorized(ATTACKER, true, A)` succeeds because `isAuthorized[A][ecrecoverAuthorizer] == true` (precondition 1).

**Exploit flow:**

| Step | Action |
|------|--------|
| 0 | A calls `midnight.setIsAuthorized(ecrecoverAuthorizer, true, A)` and `midnight.setIsAuthorized(ATTACKER, true, A)` (e.g., authorizing a keeper). |
| 1 | A signs off-chain: `Authorization{authorizer=A, authorized=LEGITIMATE_OPERATOR, isAuthorized=true, nonce=N}` and distributes the signature. |
| 2 | ATTACKER constructs `Authorization{authorizer=A, authorized=ATTACKER, isAuthorized=true, nonce=N}` and signs it with ATTACKER's own key. |
| 3 | ATTACKER calls `ecrecoverAuthorizer.setIsAuthorized(authAttacker, sigAttacker)`. |
| 4 | Line 26: `nonce[A]` advances N → N+1. Lines 33–36: `isAuthorized[A][ATTACKER] == true` → passes. `IMidnight.setIsAuthorized(ATTACKER, true, A)` succeeds (no-op on state, but transaction completes). |
| 5 | LEGITIMATE_OPERATOR submits A's signed authorization → reverts `InvalidNonce` because `nonce[A]` is now N+1. |

**Why existing checks fail** — The `Unauthorized` check is satisfied by the delegation branch; it does not distinguish between "the authorizer is signing their own nonce" and "a delegate is consuming the authorizer's nonce on a struct the delegate constructed themselves." There is no guard preventing an authorized party from targeting `authorization.authorizer = A` with a nonce they did not generate.

The `Midnight.setIsAuthorized` call at line 47 also succeeds because `msg.sender` is `ecrecoverAuthorizer` and `isAuthorized[A][ecrecoverAuthorizer] == true` (required precondition for any EcrecoverAuthorizer user), confirmed at `src/Midnight.sol` line 732: `require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized())`.

## Impact Explanation
A's off-chain signed authorization with nonce=N is permanently invalidated. LEGITIMATE_OPERATOR cannot submit it; A must re-sign with nonce=N+1 and redistribute. ATTACKER can repeat this indefinitely (each time submitting a no-op self-authorization `isAuthorized=true` for themselves), making it impossible for A to successfully delegate to any third party through `EcrecoverAuthorizer` as long as ATTACKER remains authorized. A's only escape is to revoke ATTACKER's authorization via a direct on-chain call to `midnight.setIsAuthorized`, but ATTACKER can front-run that revocation with one additional nonce burn before the revocation lands. This constitutes a persistent, repeatable DoS on A's off-chain authorization mechanism.

## Likelihood Explanation
**Preconditions:**
1. A has authorized `ecrecoverAuthorizer` in Midnight — required for any EcrecoverAuthorizer user.
2. A has authorized ATTACKER in Midnight at any prior point (e.g., a market maker, keeper, or liquidation bot).

Both are realistic for any active protocol user. `Midnight.sol` lines 105–109 document that "authorized accounts can authorize other accounts on behalf of the user," and that "other contracts might re-use Midnight's authorization mapping too (e.g. ratifiers and authorizers)." Users who authorize counterparties for position management do not expect those counterparties to also gain nonce-burning capability in `EcrecoverAuthorizer`. The attack is repeatable with zero cost beyond gas.

## Recommendation
Remove the delegation branch from `EcrecoverAuthorizer.setIsAuthorized`. Only the `authorization.authorizer` should be permitted to sign their own nonce. Delegates who are already authorized on-chain can call `Midnight.setIsAuthorized` directly without needing the off-chain signature path. The fix is:

```solidity
require(signer == authorization.authorizer, Unauthorized());
```

This eliminates the mismatch between the nonce owner and the permitted signer set. If delegation via `EcrecoverAuthorizer` is a desired feature, the nonce must be keyed to the signer rather than the authorizer, and the struct must bind the signer explicitly so a delegate cannot construct an arbitrary struct targeting a different authorizer's nonce.

## Proof of Concept

**Minimal Foundry test plan:**

1. Deploy `Midnight` and `EcrecoverAuthorizer`.
2. Create addresses: `A` (victim), `ATTACKER`, `LEGITIMATE_OPERATOR`.
3. `vm.prank(A)`: call `midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, A)`.
4. `vm.prank(A)`: call `midnight.setIsAuthorized(ATTACKER, true, A)`.
5. A signs off-chain: `Authorization{authorizer=A, authorized=LEGITIMATE_OPERATOR, isAuthorized=true, nonce=0, deadline=block.timestamp+1 days}` → `sigA`.
6. ATTACKER constructs `Authorization{authorizer=A, authorized=ATTACKER, isAuthorized=true, nonce=0, deadline=block.timestamp+1 days}` and signs with ATTACKER's private key → `sigAttacker`.
7. `vm.prank(ATTACKER)`: call `ecrecoverAuthorizer.setIsAuthorized(authAttacker, sigAttacker)` → expect success.
8. Assert `ecrecoverAuthorizer.nonce(A) == 1`.
9. `vm.prank(LEGITIMATE_OPERATOR)`: call `ecrecoverAuthorizer.setIsAuthorized(authA, sigA)` → expect revert `InvalidNonce`.