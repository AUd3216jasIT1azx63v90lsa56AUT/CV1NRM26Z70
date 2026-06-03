Audit Report

## Title
Authorized Midnight Agent Can Grief Victim's Pending Pre-Signed Authorization by Consuming the Current Nonce with a No-Op Re-Authorization - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` permits any address for which `IMidnight.isAuthorized(authorizer, signer)` returns `true` to submit a signed `Authorization` struct on behalf of the authorizer. Because the nonce is incremented unconditionally on every successful execution and no idempotency guard exists, an attacker who is already an authorized Midnight agent of the victim can submit a no-op re-authorization to burn the victim's current nonce, permanently invalidating any pending pre-signed authorization the victim has broadcast off-chain. The attack is cheap, repeatable, and requires no privileges beyond being an existing authorized agent.

## Finding Description

**Root cause:** The signer check at line 34 of `src/periphery/EcrecoverAuthorizer.sol` accepts two classes of signer: the authorizer themselves, or any address the authorizer has previously authorized on Midnight. The nonce at line 26 is incremented unconditionally on every successful call. There is no idempotency guard preventing a no-op re-authorization from consuming the nonce.

**Exact code path:**

```solidity
// src/periphery/EcrecoverAuthorizer.sol
Line 26: require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
Line 33-36: require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
Line 46-47: IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
```

`Midnight.setIsAuthorized` at line 733 writes the value unconditionally with no idempotency check:

```solidity
// src/Midnight.sol
Line 733: isAuthorized[onBehalf][authorized] = newIsAuthorized;
```

**Attacker-controlled inputs:**
- `authorization.authorizer` = victim `V`
- `authorization.authorized` = any address already authorized by `V` (e.g., attacker `A` itself) → no-op
- `authorization.isAuthorized` = `true`
- `authorization.nonce` = `N` (current `nonce[V]`)
- `authorization.deadline` = any future timestamp
- `signature` = attacker `A`'s own ECDSA signature over the above struct

**Exploit flow:**
1. Precondition: `midnight.isAuthorized(V, A) == true` (attacker is an authorized agent of victim).
2. Precondition: `midnight.isAuthorized(V, address(ecrecoverAuthorizer)) == true` (required for `EcrecoverAuthorizer` to call `midnight.setIsAuthorized` on behalf of `V`; this is a necessary operational state for any user of `EcrecoverAuthorizer`).
3. Victim `V` broadcasts a pre-signed `Authorization` with `nonce=N` off-chain.
4. Attacker `A` constructs `Authorization(authorizer=V, authorized=A, isAuthorized=true, nonce=N, deadline=future)` and signs it with their own key.
5. Attacker calls `ecrecoverAuthorizer.setIsAuthorized(auth, sig)`.
6. Line 25: deadline check passes.
7. Line 26: `N == nonce[V]` passes; `nonce[V]` incremented to `N+1`.
8. Lines 28–31: digest computed; `ecrecover` returns `A`.
9. Lines 33–36: `IMidnight(MIDNIGHT).isAuthorized(V, A) == true` → passes.
10. Lines 46–47: `midnight.setIsAuthorized(A, true, V)` → no-op (already authorized, value unchanged).
11. Victim's pending pre-signed authorization (nonce `N`) now reverts with `InvalidNonce` when submitted.

**Why existing checks fail:**
- The `Unauthorized` check (lines 33–36) is designed to allow agents to act on behalf of the authorizer, but it does not distinguish between state-changing and no-op re-authorizations.
- There is no idempotency guard: `midnight.setIsAuthorized` at line 733 writes the same value unconditionally, and the nonce has already been consumed.
- The Certora spec `EcrecoverAuthorizer.spec` rule `effects` (line 19) only asserts that the nonce increments on success — it does not rule out the case where a Midnight-authorized agent (not the authorizer) triggers that increment with a no-op payload.

## Impact Explanation
Any pending pre-signed `Authorization` the victim has created and shared off-chain (e.g., with a relayer, keeper, or counterparty) is permanently invalidated at the cost of a single transaction by any of the victim's existing Midnight-authorized agents. The victim must re-sign and re-broadcast. The attacker can repeat this indefinitely, making it impossible for the victim to use `EcrecoverAuthorizer` for off-chain authorization flows as long as the attacker remains an authorized agent. This constitutes a targeted, repeatable denial-of-service against the off-chain authorization flow of `EcrecoverAuthorizer`.

## Likelihood Explanation
**Preconditions:**
1. Attacker must hold `midnight.isAuthorized(victim, attacker) == true`. This is a realistic operational state: any protocol, keeper, or counterparty the victim has previously authorized satisfies it.
2. Victim must have a pending pre-signed authorization in flight (mempool, relayer queue, or shared off-chain).
3. `EcrecoverAuthorizer` must already be authorized by the victim (so the re-authorization is a no-op). This is a necessary operational state for any user of `EcrecoverAuthorizer`. Alternatively, the attacker can pick any other already-authorized address as `authorized`.

The attack is cheap (one transaction), repeatable, and requires no special privileges beyond being an existing authorized agent — a common operational state for any user interacting with protocols built on Midnight.

## Recommendation
Add an idempotency guard in `EcrecoverAuthorizer.setIsAuthorized` to require that the resulting call to `midnight.setIsAuthorized` actually changes state:

```solidity
bool currentIsAuthorized = IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, authorization.authorized);
require(currentIsAuthorized != authorization.isAuthorized, NoStateChange());
IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
```

Alternatively, restrict the signer check to only accept the authorizer themselves (removing the `isAuthorized` delegation path), or require that the `authorized` address is not already in the target state before consuming the nonce.

## Proof of Concept
**Minimal Foundry test plan:**

```solidity
// Setup:
// 1. Deploy Midnight and EcrecoverAuthorizer.
// 2. victim.setIsAuthorized(attacker, true) on Midnight.
// 3. victim.setIsAuthorized(address(ecrecoverAuthorizer), true) on Midnight.
// 4. victim signs Authorization{authorizer: victim, authorized: someNewAddr, isAuthorized: true, nonce: 0, deadline: future}.
// 5. Victim's signed authorization is "broadcast" (stored off-chain).

// Attack:
// 6. attacker constructs Authorization{authorizer: victim, authorized: attacker, isAuthorized: true, nonce: 0, deadline: future}.
// 7. attacker signs this with their own key.
// 8. attacker calls ecrecoverAuthorizer.setIsAuthorized(attackerAuth, attackerSig).
// 9. Assert: nonce[victim] == 1 (consumed).
// 10. Assert: ecrecoverAuthorizer.setIsAuthorized(victimAuth, victimSig) reverts with InvalidNonce.
// 11. Assert: midnight.isAuthorized(victim, attacker) == true (no state change, no-op confirmed).
```