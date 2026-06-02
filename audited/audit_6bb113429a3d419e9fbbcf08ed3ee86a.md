Audit Report

## Title
Pending EcrecoverAuthorizer Signature Survives Direct Midnight Revocation — (File: `src/periphery/EcrecoverAuthorizer.sol`)

## Summary
`EcrecoverAuthorizer` maintains a per-authorizer nonce in `nonce[authorizer]` that is only incremented by successful calls through `EcrecoverAuthorizer.setIsAuthorized`. When a victim revokes an attacker's authorization by calling `Midnight.setIsAuthorized(attacker, false, victim)` directly, the `EcrecoverAuthorizer` nonce is untouched, leaving any pending signed authorization valid. The attacker can submit the withheld signature to restore their authorization, permanently defeating the revocation.

## Finding Description
**Root cause:** `EcrecoverAuthorizer.setIsAuthorized` enforces two checks before acting:

```solidity
require(block.timestamp <= authorization.deadline, Expired());          // line 25
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce()); // line 26
```

The nonce lives exclusively in `EcrecoverAuthorizer.nonce[authorizer]` and is only incremented here. `Midnight.setIsAuthorized` (lines 731–735) writes `isAuthorized[onBehalf][authorized]` but has no knowledge of and makes no change to the `EcrecoverAuthorizer` nonce. There is no `invalidateNonce()` or equivalent function in `EcrecoverAuthorizer` — confirmed by the interface at `src/periphery/interfaces/IEcrecoverAuthorizer.sol`, which exposes only `setIsAuthorized` and the `nonce` getter.

**Exploit flow:**

1. Victim calls `Midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim)` — standard setup. `isAuthorized[victim][ecrecoverAuthorizer] = true`.
2. Victim signs `Authorization{authorizer: victim, authorized: attacker, isAuthorized: true, nonce: 0, deadline: block.timestamp + 365 days}` and shares it with the attacker (off-chain delegation, intercepted mempool tx, etc.).
3. Attacker deliberately withholds the signature.
4. Victim calls `Midnight.setIsAuthorized(attacker, false, victim)`. `isAuthorized[victim][attacker]` is now `false`. `EcrecoverAuthorizer.nonce[victim]` is still `0`.
5. Attacker submits the withheld signature to `EcrecoverAuthorizer.setIsAuthorized`. Deadline check passes. Nonce check passes (`0 == 0`, nonce incremented to 1). Signature validity check passes (`signer == authorization.authorizer` since the victim signed it). `EcrecoverAuthorizer` calls `Midnight.setIsAuthorized(attacker, true, victim)`.
6. In `Midnight.setIsAuthorized`, the authorization check `isAuthorized[victim][ecrecoverAuthorizer]` is still `true` (victim never revoked it), so the call succeeds. `isAuthorized[victim][attacker]` is `true` again. Revocation is undone.

**Why existing checks fail:** The nonce prevents replay of an already-consumed signature but does not allow the victim to invalidate a pending (signed, not-yet-submitted) signature. The victim's only escape is to also revoke `ecrecoverAuthorizer`'s own authorization via `Midnight.setIsAuthorized(ecrecoverAuthorizer, false, victim)`, but nothing in the protocol enforces or documents this as a required step. The `Midnight.sol` AUTHORIZATIONS comment (lines 101–110) warns that "authorized accounts can authorize other accounts on behalf of the user," but does not warn that a direct revocation of a delegatee is insufficient when a pending `EcrecoverAuthorizer` signature exists.

## Impact Explanation
An attacker holding any pending (signed, deadline-not-expired) `EcrecoverAuthorizer` authorization for `isAuthorized: true` can restore their own authorization at any time after the victim's revocation. With restored `isAuthorized`, the attacker can call on behalf of the victim: `take` (line 346), `withdraw` (line 482), `repay` (line 505), `supplyCollateral` (line 527), `withdrawCollateral` (line 556), `setConsumed` (line 724), and `setIsAuthorized` (line 732) — enabling direct theft of funds, unauthorized position manipulation, and further authorization chaining. This constitutes unauthorized movement of assets and unauthorized state changes.

## Likelihood Explanation
Preconditions are realistic: off-chain delegation flows routinely produce signed-but-not-yet-submitted authorizations. The attacker only needs to withhold submission until after the victim's revocation transaction confirms, then submit. The attack is repeatable for every pending signature the attacker holds, is not blocked by any on-chain mechanism, and requires no privileged access — only possession of a valid signed authorization.

## Recommendation
Add a public `invalidateNonce(address authorizer)` function to `EcrecoverAuthorizer` that increments `nonce[msg.sender]`, allowing a signer to atomically invalidate all pending signatures at their current nonce. Alternatively, mirror the pattern used by EIP-2612 permit implementations and allow the authorizer to self-submit a no-op authorization (e.g., `authorized == authorizer`) solely to advance the nonce. Additionally, document in the AUTHORIZATIONS section that revoking a delegatee's authorization via `Midnight.setIsAuthorized` is insufficient if a pending `EcrecoverAuthorizer` signature exists — the authorizer contract's own authorization must also be revoked, or the nonce must be advanced.

## Proof of Concept
```solidity
// 1. victim authorizes ecrecoverAuthorizer
midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim); // from victim

// 2. victim signs Authorization{authorizer: victim, authorized: attacker, isAuthorized: true, nonce: 0, deadline: block.timestamp + 365 days}
// attacker receives signature off-chain but withholds it

// 3. victim revokes attacker directly
midnight.setIsAuthorized(attacker, false, victim); // from victim
assert(!midnight.isAuthorized(victim, attacker)); // revocation confirmed
assert(ecrecoverAuthorizer.nonce(victim) == 0);   // nonce unchanged

// 4. attacker submits withheld signature
ecrecoverAuthorizer.setIsAuthorized(authorization, signature); // from attacker
assert(midnight.isAuthorized(victim, attacker)); // revocation undone

// 5. attacker drains victim
midnight.withdrawCollateral(market, 0, collateral, victim, attacker); // from attacker
```