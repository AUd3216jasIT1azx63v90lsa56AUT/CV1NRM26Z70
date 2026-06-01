Audit Report

## Title
Authorized Agent Can Grief Victim's Nonce via Self-Deauthorization - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
Any address holding `isAuthorized[victim][attacker] == true` in `Midnight` can call `EcrecoverAuthorizer.setIsAuthorized` with a crafted `Authorization(authorizer=victim, authorized=attacker, isAuthorized=false, nonce=N)` signed by the attacker's own key. The call passes all checks, atomically increments `nonce[victim]` from N to N+1, and removes the attacker's own authorization. Any off-chain signed authorization the victim has distributed with `nonce=N` is permanently invalidated with `InvalidNonce()` on submission.

## Finding Description

**Root cause:** `EcrecoverAuthorizer.setIsAuthorized` allows any address that `Midnight.isAuthorized(authorizer, signer)` returns `true` for to act as the signer, including signing messages that target themselves as `authorized` with `isAuthorized=false`. No check prevents a delegated signer from crafting a self-deauthorization that consumes the authorizer's nonce.

**Step 1 — Nonce increment (line 26):**
```solidity
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```
`nonce[victim]` is incremented from N to N+1 as part of the `require` evaluation, before any signature verification occurs.

**Step 2 — Signature check passes (lines 31–36):**
```solidity
address signer = ecrecover(digest, signature.v, signature.r, signature.s);
require(signer != address(0), InvalidSignature());
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```
The attacker signs with their own key → `signer = attacker`. Branch 1: `attacker == victim` → false. Branch 2: `isAuthorized[victim][attacker]` → **true** by precondition. Check passes.

**Step 3 — Downstream call (lines 46–47):**
```solidity
IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
```
Calls `Midnight.setIsAuthorized(attacker, false, victim)`.

**Step 4 — Midnight authorization check (line 732):**
```solidity
require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```
`msg.sender = EcrecoverAuthorizer`, `onBehalf = victim`. For the EcrecoverAuthorizer flow to function at all, `isAuthorized[victim][EcrecoverAuthorizer]` must be `true`. Check passes. Result: `isAuthorized[victim][attacker] = false`.

**Net state after attack:**
- `nonce[victim]` = N+1
- `isAuthorized[victim][attacker]` = false
- All off-chain signed `Authorization(authorizer=victim, ..., nonce=N)` → permanently `InvalidNonce()`

## Impact Explanation
Any pending gasless authorization the victim has signed and distributed (e.g., to a relayer, bundler, or ratifier) with `nonce=N` is rendered permanently unsubmittable. The victim must detect the invalidation, re-sign with `nonce=N+1`, and redistribute. If the attacker is a routinely re-authorized address (e.g., a trusted relayer the victim keeps granting access), the attacker can repeat the pattern on each new nonce, causing sustained DoS on the victim's gasless authorization workflow. No funds are directly stolen, but the integrity of the off-chain signing flow is broken and can be continuously disrupted.

## Likelihood Explanation
**Preconditions:** (1) `isAuthorized[victim][EcrecoverAuthorizer] == true` — required for any EcrecoverAuthorizer usage; (2) `isAuthorized[victim][attacker] == true` — normal operational state for any user who has authorized a relayer, bundler, ratifier, or callback contract. Both conditions are standard for users of the gasless authorization flow. **Feasibility:** One transaction, no capital, no oracle dependency. **Repeatability:** One-shot per authorization grant (attacker loses their own authorization), but if the victim re-authorizes the attacker (e.g., a trusted relayer), the attack is repeatable indefinitely.

## Recommendation
Add a check in `EcrecoverAuthorizer.setIsAuthorized` that prevents a delegated signer from signing a message where `authorization.authorized == signer && authorization.isAuthorized == false`. Alternatively, restrict the signer to only be `authorization.authorizer` (removing the delegated-signer path entirely for this function), or introduce a separate nonce namespace per `(authorizer, signer)` pair so a delegated signer cannot consume the authorizer's global nonce. The minimal targeted fix:

```solidity
require(
    signer == authorization.authorizer ||
    (IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer) &&
     !(authorization.authorized == signer && !authorization.isAuthorized)),
    Unauthorized()
);
```

## Proof of Concept
1. Deploy `Midnight` and `EcrecoverAuthorizer`.
2. Victim calls `Midnight.setIsAuthorized(EcrecoverAuthorizer, true, victim)` and `Midnight.setIsAuthorized(attacker, true, victim)`.
3. Victim signs off-chain: `Authorization(authorizer=victim, authorized=someAddress, isAuthorized=true, nonce=0, deadline=future)` → stores as `pendingAuth`.
4. Attacker constructs `Authorization(authorizer=victim, authorized=attacker, isAuthorized=false, nonce=0, deadline=future)`, signs with attacker's key, calls `EcrecoverAuthorizer.setIsAuthorized`.
5. Assert: `nonce[victim] == 1`, `isAuthorized[victim][attacker] == false`.
6. Submit `pendingAuth` (nonce=0) → reverts with `InvalidNonce()`.
7. Victim re-authorizes attacker; attacker repeats from step 4 with nonce=1. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L26-26)
```text
        require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```

**File:** src/periphery/EcrecoverAuthorizer.sol (L31-36)
```text
        address signer = ecrecover(digest, signature.v, signature.r, signature.s);
        require(signer != address(0), InvalidSignature());
        require(
            signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
            Unauthorized()
        );
```

**File:** src/periphery/EcrecoverAuthorizer.sol (L46-47)
```text
        IMidnight(MIDNIGHT)
            .setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
```

**File:** src/Midnight.sol (L731-734)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
```
