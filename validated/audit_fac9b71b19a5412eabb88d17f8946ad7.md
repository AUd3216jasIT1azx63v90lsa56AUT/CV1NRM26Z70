Audit Report

## Title
Authorized agent can consume victim's EcrecoverAuthorizer nonce without consent, invalidating pending pre-signed authorizations - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` increments `nonce[authorization.authorizer]` before verifying the signer, and the signer check accepts any address for whom `IMidnight.isAuthorized(authorizer, signer)` returns true. An attacker holding a live Midnight authorization from a victim can self-sign a no-op `Authorization` struct using the victim's current nonce, submit it, and permanently invalidate any pending pre-signed authorization the victim had distributed to a relayer. No funds are stolen, but time-sensitive operations gated on pre-signed authorizations can be blocked indefinitely.

## Finding Description
**Exact code path — `src/periphery/EcrecoverAuthorizer.sol`:**

Line 26 increments the nonce unconditionally, keyed on `authorization.authorizer`, before any signer check:
```solidity
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```

Lines 33–36 accept any address that Midnight considers authorized by the victim, not exclusively the authorizer themselves:
```solidity
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

**Root cause:** The nonce is keyed on `authorization.authorizer` (the victim), but the signer validity check accepts any address that Midnight considers authorized by the victim. These two checks are decoupled: an authorized agent can produce a valid signature over an `Authorization` struct naming the victim as `authorizer`, satisfying both checks while consuming the victim's nonce without the victim's specific consent for that nonce consumption.

**Exploit flow:**
1. Victim pre-signs `Authorization(authorizer=victim, authorized=X, isAuthorized=true, nonce=N, deadline=T)` and hands it to a relayer.
2. Attacker (holding `isAuthorized[victim][attacker] == true` in Midnight) constructs `Authorization(authorizer=victim, authorized=victim, isAuthorized=true, nonce=N, deadline=T')` and signs it with their own key.
3. Attacker calls `EcrecoverAuthorizer.setIsAuthorized(auth, sig)`.
4. Line 26 passes: `N == nonce[victim]`, nonce incremented to `N+1`.
5. Lines 33–36 pass: `ecrecover(...)` returns attacker's address; `IMidnight.isAuthorized(victim, attacker)` is true.
6. Line 47 executes: `Midnight.setIsAuthorized(victim, true, victim)` — a no-op state change (`isAuthorized[victim][victim] = true`).
7. Victim's pre-signed authorization with nonce `N` now reverts with `InvalidNonce()` when submitted by the relayer.

**Why existing checks fail:** The `Unauthorized()` guard was designed to prevent strangers from acting on behalf of an authorizer. It does not distinguish between "the authorizer consented to this specific nonce consumption" and "the signer happens to be an authorized agent." The nonce increment occurs before the signer check and is not gated on the signer being the authorizer themselves.

The Midnight.sol `setIsAuthorized` correctly applies the same authorization model (`isAuthorized[onBehalf][msg.sender]`), confirming authorized agents are permitted to act on behalf of the authorizer throughout the protocol. The flaw is specific to the nonce-consumption side effect in `EcrecoverAuthorizer`, which the victim did not consent to when granting the agent authorization in Midnight for a different purpose (e.g., trading via `take`).

The Midnight.sol documentation warns that other contracts may re-use the authorization mapping, but this warning does not cover the specific consequence of nonce invalidation in `EcrecoverAuthorizer`, and users cannot reasonably anticipate that granting a trading bot authorization in Midnight also grants it the ability to burn their pre-signed `EcrecoverAuthorizer` nonces.

## Impact Explanation
Any pending pre-signed `Authorization` with nonce `N` that the victim distributed — intended to enable a counterparty to call `take`, `repay`, `withdraw`, `liquidate`, or `claimFee` on their behalf via `EcrecoverAuthorizer` — is permanently invalidated at the cost of a single transaction. The victim must re-sign and redistribute a new authorization. The attacker can repeat this every time the victim issues a new pre-signed authorization, creating a sustained, low-cost denial-of-service against all signature-gated market actions routed through `EcrecoverAuthorizer`. No funds are directly stolen, but critical time-sensitive operations (e.g., liquidation avoidance, repayment via relayer) can be blocked indefinitely. Severity: **Medium** (DoS with no direct fund loss, but with concrete time-sensitive operational impact).

## Likelihood Explanation
**Preconditions:**
- Attacker must hold `isAuthorized[victim][attacker] == true` in Midnight. This is realistic: users authorize relayers, smart contracts (`EcrecoverRatifier`, callback contracts), or trading bots as part of normal protocol usage.
- Victim must have a pending pre-signed authorization in circulation.

**Feasibility:** Single transaction, no capital required, no oracle manipulation, no flash loan. Repeatable indefinitely as long as the authorization relationship persists. The attacker does not need to front-run; they only need to submit before the relayer does.

## Recommendation
Gate the nonce increment on the signer being the authorizer themselves, or move the nonce increment after the signer check and require `signer == authorization.authorizer` (disallowing agents from consuming the authorizer's nonce). Alternatively, key the nonce on the signer rather than the authorizer, so each agent has their own nonce space and cannot interfere with the authorizer's nonce.

A minimal fix:
```solidity
// Verify signer first, then consume nonce only if signer == authorizer
address signer = ecrecover(digest, signature.v, signature.r, signature.s);
require(signer != address(0), InvalidSignature());
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
// Only the authorizer themselves may consume the authorizer's nonce
require(signer == authorization.authorizer, Unauthorized());
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```
Or, if agent-signed authorizations are intentionally supported, key the nonce on the signer: `nonce[signer]++`.

## Proof of Concept
**Minimal manual steps:**
1. Deploy `Midnight` and `EcrecoverAuthorizer`.
2. Have `victim` call `Midnight.setIsAuthorized(attacker, true, victim)` (simulating a normal trading bot authorization).
3. Have `victim` sign `Authorization(authorizer=victim, authorized=relayTarget, isAuthorized=true, nonce=0, deadline=T)` and give it to a relayer (do not submit yet).
4. Have `attacker` construct `Authorization(authorizer=victim, authorized=victim, isAuthorized=true, nonce=0, deadline=T')`, sign it with attacker's key, and call `EcrecoverAuthorizer.setIsAuthorized(auth, sig)`.
5. Observe: `nonce[victim]` is now `1`; `Midnight.isAuthorized[victim][victim] == true` (no-op).
6. Have the relayer submit the victim's pre-signed authorization from step 3: it reverts with `InvalidNonce()`.
7. Repeat from step 3 to demonstrate indefinite repeatability.