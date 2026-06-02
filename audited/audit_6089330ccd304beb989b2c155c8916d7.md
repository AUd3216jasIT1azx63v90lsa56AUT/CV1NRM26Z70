Audit Report

## Title
Authorized-party nonce front-run invalidates victim's pending EcrecoverAuthorizer authorization - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` increments the authorizer's nonce on line 26 as a side-effect of the `require` check, before the signer identity is verified on lines 33–36. Because the signer check accepts any address for which `isAuthorized[authorizer][signer]` is true on Midnight, an attacker who holds a prior Midnight authorization from the victim can craft an arbitrary `Authorization` struct with the victim's current nonce, sign it with their own key, and submit it first — consuming the nonce and causing the victim's pending transaction to revert with `InvalidNonce`.

## Finding Description
**Code path:** `src/periphery/EcrecoverAuthorizer.sol`, `setIsAuthorized`, lines 24–47.

```solidity
// line 26 — nonce consumed BEFORE signer is checked
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());

// lines 33-36 — any Midnight-authorized party is accepted as signer
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

**Root cause:** The nonce increment is committed on line 26 as a side-effect of the `require` passing. The subsequent signer check on lines 33–36 accepts not only the authorizer themselves but also any address for which `isAuthorized[authorizer][signer]` is true on Midnight. There is no binding between a nonce and a specific authorization payload from the authorizer's own intent; any authorized party can craft an arbitrary `Authorization` struct with the victim's current nonce and a valid signature from their own key.

**Exploit flow:**
1. Precondition: `isAuthorized[victim][attacker] == true` on Midnight (attacker was previously authorized by victim for any reason). `isAuthorized[victim][EcrecoverAuthorizer] == true` on Midnight (required for `EcrecoverAuthorizer` to act on victim's behalf).
2. Victim signs `Authorization{authorizer=victim, authorized=X, isAuthorized=true, nonce=N, deadline=T}` and broadcasts to `EcrecoverAuthorizer.setIsAuthorized`.
3. Attacker observes the mempool and front-runs with `Authorization{authorizer=victim, authorized=attacker_addr, isAuthorized=true, nonce=N, deadline=T'}` signed by the attacker's own key.
4. Attacker's tx executes first: line 26 passes (`N == N`), nonce becomes `N+1`; lines 33–36 pass because `isAuthorized[victim][attacker]` is true; attacker's chosen authorization is written to Midnight via line 46–47.
5. Victim's tx executes: line 26 fails (`N != N+1`) → `InvalidNonce` revert.

**Why existing checks fail:** The `InvalidNonce` check only prevents replay of an already-consumed nonce; it does not prevent a different authorized party from consuming the nonce first with a different payload. The `Unauthorized` check is the intended delegation feature but is the enabler of the attack.

## Impact Explanation
The victim's pending authorization is permanently invalidated for nonce N, forcing re-signing. The attacker's chosen authorization — which can target any `authorized` address with any `isAuthorized` value — takes effect on Midnight. The attacker can repeat this for every subsequent nonce as long as they remain authorized, making the `EcrecoverAuthorizer` path permanently unusable for the victim without first revoking the attacker's Midnight authorization via the direct `Midnight.setIsAuthorized` call. This constitutes unauthorized state changes and permanent degradation of the `EcrecoverAuthorizer` service for the victim.

Note: because `isAuthorized[victim][attacker]` being true already permits the attacker to call `Midnight.setIsAuthorized` directly on the victim's behalf, the incremental capability from this specific attack is primarily the nonce-consumption griefing and the ability to substitute a different payload than what the victim signed — the victim cannot prevent this substitution through `EcrecoverAuthorizer` alone.

## Likelihood Explanation
The precondition — attacker holds a Midnight authorization from the victim — is reachable via any prior `setIsAuthorized` call (direct or via `EcrecoverAuthorizer`). Ratifiers, liquidators, and other protocol participants are commonly authorized, as noted in the Midnight contract's AUTHORIZATIONS section. Front-running is straightforward on any chain with a public mempool. The attack is repeatable at zero cost beyond gas.

## Recommendation
**Primary fix:** Remove the `isAuthorized` delegation branch from `EcrecoverAuthorizer`. Only the authorizer themselves should be permitted to sign in this contract; delegation is already available via the direct `Midnight.setIsAuthorized` path. Change lines 33–36 to:

```solidity
require(signer == authorization.authorizer, Unauthorized());
```

**Secondary fix (defense-in-depth):** Move the nonce increment after the signer check so that invalid signer attempts do not consume the nonce:

```solidity
// verify signer first
require(signer == authorization.authorizer, Unauthorized());
// then consume nonce
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```

**Alternative:** If delegation via `EcrecoverAuthorizer` is intentionally desired, add a `submitter` field to the `Authorization` struct that the authorizer explicitly signs over, binding the nonce to a specific submitter address and preventing unauthorized parties from substituting a different payload.

## Proof of Concept
**Minimal Foundry test plan:**

```solidity
// Setup:
// 1. Deploy Midnight and EcrecoverAuthorizer.
// 2. victim authorizes EcrecoverAuthorizer on Midnight.
// 3. victim authorizes attacker on Midnight (simulating any prior protocol interaction).

// Attack:
// 4. victim signs Authorization{authorizer=victim, authorized=X, isAuthorized=true, nonce=0, deadline=T}.
// 5. Before victim's tx, attacker calls setIsAuthorized with
//    Authorization{authorizer=victim, authorized=attacker, isAuthorized=true, nonce=0, deadline=T'}
//    signed by attacker's key.
// 6. Assert: attacker's tx succeeds; nonce[victim] == 1; isAuthorized[victim][attacker] == true on Midnight.
// 7. Submit victim's original tx.
// 8. Assert: victim's tx reverts with InvalidNonce().
// 9. Assert: isAuthorized[victim][X] == false (victim's intended authorization never took effect).
```