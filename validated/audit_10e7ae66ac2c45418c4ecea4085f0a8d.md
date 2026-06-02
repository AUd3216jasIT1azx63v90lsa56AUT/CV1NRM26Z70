Audit Report

## Title
Authorized Midnight Agent Can Grief Victim's Pending Pre-Signed Authorization by Consuming the Current Nonce with a No-Op Re-Authorization - (File: `src/periphery/EcrecoverAuthorizer.sol`)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` permits any address holding `midnight.isAuthorized(victim, attacker) == true` to sign and submit an `Authorization` struct on behalf of the victim. Because the nonce at line 26 is incremented unconditionally on every successful execution and `Midnight.setIsAuthorized` performs an unconditional write with no idempotency guard, an attacker who is already an authorized agent of the victim can submit a no-op re-authorization (re-authorizing an already-authorized address) to burn the victim's current nonce, permanently invalidating any pending pre-signed authorization the victim has broadcast off-chain. The attack is cheap, repeatable, and requires no privileges beyond being an existing authorized agent.

## Finding Description

**Root cause:** The nonce increment at line 26 of `src/periphery/EcrecoverAuthorizer.sol` is unconditional on every successful call. The signer check at lines 33–36 accepts two classes of signer: the authorizer themselves, or any address the authorizer has previously authorized on Midnight. There is no idempotency guard preventing a no-op re-authorization (e.g., re-authorizing an already-authorized address) from consuming the nonce.

**Exact code path:**
- Line 26: `require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());` — nonce incremented unconditionally on success.
- Lines 33–36: `signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer)` — accepts any existing authorized agent as a valid signer.
- Lines 46–47: `IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer)` — unconditional write; `Midnight.setIsAuthorized` at line 733 of `src/Midnight.sol` performs `isAuthorized[onBehalf][authorized] = newIsAuthorized` with no idempotency check.

**Attacker-controlled inputs:**
- `authorization.authorizer` = victim `V`
- `authorization.authorized` = any address already authorized by `V` (e.g., `address(ecrecoverAuthorizer)`) → no-op
- `authorization.isAuthorized` = `true`
- `authorization.nonce` = `N` (current `nonce[V]`)
- `authorization.deadline` = any future timestamp
- `signature` = attacker `A`'s own ECDSA signature over the above struct

**Exploit flow:**
1. Precondition: `midnight.isAuthorized(V, A) == true` (attacker is an authorized agent of victim).
2. Precondition: some address `X` already satisfies `midnight.isAuthorized(V, X) == true` (e.g., `address(ecrecoverAuthorizer)` itself, which must be authorized for the contract to function).
3. Victim `V` broadcasts a pre-signed `Authorization` with `nonce=N` off-chain (e.g., to a relayer or mempool).
4. Attacker `A` constructs `Authorization(authorizer=V, authorized=X, isAuthorized=true, nonce=N, deadline=future)` and signs it with their own key.
5. Attacker calls `ecrecoverAuthorizer.setIsAuthorized(auth, sig)`.
6. Line 25: deadline check passes.
7. Line 26: `N == nonce[V]` passes; `nonce[V]` incremented to `N+1`.
8. Lines 28–31: digest computed; `ecrecover` returns `A`.
9. Lines 33–36: `IMidnight(MIDNIGHT).isAuthorized(V, A) == true` → passes.
10. Lines 46–47: `midnight.setIsAuthorized(X, true, V)` → no-op (already authorized).
11. Victim's pending pre-signed authorization (nonce `N`) now reverts with `InvalidNonce` when submitted.

**Why existing checks fail:**
- The `Unauthorized` check (lines 33–36) is designed to allow agents to act on behalf of the authorizer, but it does not distinguish between state-changing and no-op re-authorizations.
- There is no idempotency guard: `midnight.setIsAuthorized` at lines 46–47 writes the same value unconditionally, and the nonce has already been consumed.
- The Certora spec `certora/specs/EcrecoverAuthorizer.spec` rule `effects` (line 19) only asserts that the nonce increments on success — it does not rule out the case where a Midnight-authorized agent (not the authorizer) triggers that increment with a no-op payload.

## Impact Explanation
Any pending pre-signed `Authorization` the victim has created and shared off-chain (e.g., with a relayer, keeper, or counterparty) is permanently invalidated at the cost of a single transaction by any of the victim's existing Midnight-authorized agents. The victim must re-sign and re-broadcast. The attacker can repeat this indefinitely, making it impossible for the victim to use `EcrecoverAuthorizer` for off-chain authorization flows as long as the attacker remains an authorized agent. This constitutes a targeted, repeatable denial-of-service against the off-chain authorization flow of `EcrecoverAuthorizer`.

## Likelihood Explanation
**Preconditions:**
1. Attacker must hold `midnight.isAuthorized(victim, attacker) == true`. This is a realistic operational state: any protocol, keeper, or counterparty the victim has previously authorized satisfies it. This is a user-level permission, not a protocol-level privilege (not governance/admin/owner).
2. Victim must have a pending pre-signed authorization in flight (mempool, relayer queue, or shared off-chain).
3. Attacker needs to pick any address already authorized by the victim as the `authorized` field to make the call a no-op. If `EcrecoverAuthorizer` itself is already authorized (a required setup for the contract to function), it is a trivially available target.

The attack is cheap (one transaction), repeatable, and requires no special privileges beyond being an existing authorized agent — a common operational state for any user interacting with protocols built on Midnight.

## Recommendation
Add an idempotency guard in `EcrecoverAuthorizer.setIsAuthorized` before the nonce is consumed, reverting if the authorization state would not change:

```solidity
bool currentState = IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, authorization.authorized);
require(currentState != authorization.isAuthorized, NoStateChange());
```

Alternatively, restrict the signer to only the authorizer themselves (remove the agent-signing path), since the primary use case of `EcrecoverAuthorizer` is off-chain signing by the authorizer. If agent signing is intentional, document the trust implications clearly and add the idempotency guard.

## Proof of Concept
```solidity
// Setup
vm.prank(victim);
midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim); // required for EcrecoverAuthorizer to work
vm.prank(victim);
midnight.setIsAuthorized(attacker, true, victim); // attacker is an authorized agent

// Victim creates and shares a pre-signed authorization off-chain
Authorization memory victimAuth = Authorization({
    authorizer: victim,
    authorized: someNewAddress,
    isAuthorized: true,
    nonce: ecrecoverAuthorizer.nonce(victim), // N
    deadline: block.timestamp + 1 days
});
Signature memory victimSig = signAuthorization(victimAuth, victim);

// Attacker front-runs with a no-op re-authorization signed by attacker
Authorization memory attackAuth = Authorization({
    authorizer: victim,
    authorized: address(ecrecoverAuthorizer), // already authorized → no-op
    isAuthorized: true,
    nonce: ecrecoverAuthorizer.nonce(victim), // same N
    deadline: block.timestamp + 1 days
});
Signature memory attackSig = signAuthorization(attackAuth, attacker); // attacker signs

vm.prank(attacker);
ecrecoverAuthorizer.setIsAuthorized(attackAuth, attackSig); // succeeds, nonce[victim] → N+1

// Victim's pre-signed authorization now reverts
vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
ecrecoverAuthorizer.setIsAuthorized(victimAuth, victimSig);
```