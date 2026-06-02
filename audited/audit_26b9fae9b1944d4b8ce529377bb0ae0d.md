All six code claims check out against the actual source. Here is the validation result:

---

Audit Report

## Title
`claimContinuousFee` reverts via arithmetic underflow when bad-debt liquidation front-runs the feeClaimer - (File: `src/Midnight.sol`)

## Summary
`claimContinuousFee` subtracts `amount` from `continuousFeeCredit` with no guard that `amount <= continuousFeeCredit`. A zero-cost bad-debt liquidation (`seizedAssets=0, repaidUnits=0`) — explicitly documented by the protocol — atomically reduces `continuousFeeCredit` via the proportional slash formula. Any unprivileged caller can front-run the feeClaimer's pending claim and cause it to revert with a Solidity 0.8 arithmetic underflow, persistently griefing protocol fee collection at gas cost only.

## Finding Description
**Root cause — `claimContinuousFee`** (`src/Midnight.sol:318`):
```solidity
_marketState.continuousFeeCredit -= UtilsLib.toUint128(amount);
```
No `require(amount <= _marketState.continuousFeeCredit)` guard exists. Solidity 0.8 checked arithmetic reverts on underflow.

**Bad-debt slash — `liquidate`** (`src/Midnight.sol:626–640`):
```solidity
if (badDebt > 0) {
    ...
    _marketState.continuousFeeCredit = _lossFactor < type(uint128).max
        ? UtilsLib.toUint128(
            _marketState.continuousFeeCredit
                .mulDivDown(type(uint128).max - _marketState.lossFactor,
                            type(uint128).max - _lossFactor)
          )
        : 0;
}
```
Because `newLossFactor > oldLossFactor`, the ratio `(MAX − newLF) / (MAX − oldLF) < 1`, so `continuousFeeCredit` is strictly reduced on every bad-debt event.

**Zero-cost liquidation path** (`src/Midnight.sol:577–578`, `src/Midnight.sol:595`, `src/Midnight.sol:643`):
The protocol explicitly documents that `seizedAssets=0, repaidUnits=0` realizes bad debt with zero tokens transferred. `UtilsLib.atMostOneNonZero(0, 0)` returns `true` (assembly: `or(iszero(0), iszero(0)) = 1`), so the `InconsistentInput` check passes. The `if (repaidUnits > 0 || seizedAssets > 0)` block at line 643 is skipped entirely — no tokens move — but the bad-debt slash at lines 626–640 executes unconditionally whenever `badDebt > 0`.

**Exploit flow**:
1. `continuousFeeCredit = X`. feeClaimer broadcasts `claimContinuousFee(market, X, receiver)`.
2. Attacker front-runs with `liquidate(market, 0, 0, 0, borrower, false, receiver, address(0), "")` — zero tokens spent, gas only. `continuousFeeCredit` is reduced to `X' < X`.
3. feeClaimer's transaction executes: `X' -= X` → arithmetic underflow → revert.

**Why existing checks fail**:
- `liquidatorGate` check (`src/Midnight.sol:598`): only restricts callers when `market.liquidatorGate != address(0)`; default markets have no gate, leaving `liquidate` fully permissionless.
- Certora spec (`certora/specs/Role.spec:264`) encodes `amount <= continuousFeeCreditBefore` as a necessary non-revert condition, confirming no on-chain guard enforces this invariant — the spec documents the gap rather than closing it.

## Impact Explanation
Protocol fee collection via `claimContinuousFee` is persistently blocked. No funds are permanently lost — `continuousFeeCredit` is reduced (not stolen) — but the feeClaimer must re-read state and resubmit on every attempt, and the attack can be replayed immediately on each resubmission as long as any bad-debt-eligible position exists. This constitutes a sustained, zero-cost denial-of-service against a core protocol revenue function, matching the "service unavailability or severe degradation under realistic attacker input" impact class in `RESEARCHER.md`.

## Likelihood Explanation
**Attacker cost**: gas only — no tokens required. **Preconditions**: (a) a borrower position with `debt > 0` and `originalDebt > maxDebt` (bad debt) exists, and (b) the feeClaimer submits `amount = continuousFeeCredit` (the natural "claim all" call). Both are routine market conditions. **Repeatability**: the attack can be replayed on every subsequent claim attempt until all bad-debt positions are exhausted. **Access**: any unprivileged address can call `liquidate` on markets without a `liquidatorGate`.

## Recommendation
Add an explicit cap in `claimContinuousFee` before the subtraction:

```solidity
amount = UtilsLib.min(amount, _marketState.continuousFeeCredit);
```

Or add a revert guard:

```solidity
require(amount <= _marketState.continuousFeeCredit, InsufficientContinuousFeeCredit());
```

The `min` variant is preferable as it makes "claim all available" atomic and front-run-resistant without requiring the feeClaimer to re-read state. Alternatively, expose a `claimAllContinuousFee` function that reads and claims the current balance atomically.

## Proof of Concept
1. Deploy a market with no `liquidatorGate`.
2. Create a borrower position with `debt > 0` and collateral value such that `originalDebt > maxDebt` and `badDebt > 0` (collateral value at `maxLif` < debt).
3. Accrue `continuousFeeCredit = X > 0` via normal protocol operation.
4. As attacker, call `liquidate(market, 0, 0, 0, borrower, false, attacker, address(0), "")`. Verify `continuousFeeCredit` is now `X' < X` and no tokens were transferred.
5. As feeClaimer, call `claimContinuousFee(market, X, receiver)`. Observe revert with arithmetic underflow.
6. Repeat step 4 immediately after feeClaimer re-reads state and resubmits, demonstrating repeatability.