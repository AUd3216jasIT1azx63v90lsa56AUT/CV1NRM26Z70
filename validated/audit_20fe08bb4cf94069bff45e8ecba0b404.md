All seven code references have been verified against the actual source. Here is the validated output:

---

Audit Report

## Title
Systematic per-lender credit over-slash via compounded `mulDivDown` rounding in `updatePositionView` — (File: src/Midnight.sol)

## Summary
In `liquidate`, the surviving multiplier stored in `lossFactor` is floored by `mulDivDown`, making `lossFactor` strictly larger than the exact value. In `updatePositionView`, `postSlashCredit` applies a second `mulDivDown` against that already-floored `lossFactor`. Both floors compound in the same direction, causing lenders to collectively lose more credit than the actual bad debt, with the difference permanently orphaned inside `totalUnits` and unclaimable by any party.

## Finding Description

**Root cause — double floor in the same direction:**

`lossFactor` update (`src/Midnight.sol:631-633`): [1](#0-0) 

Let `MAX = type(uint128).max`, `A = MAX − old_lossFactor`. The surviving multiplier stored is `⌊A·(T−B)/T⌋`, so `(MAX − new_lossFactor) = ⌊A·(T−B)/T⌋ ≤ exact`. The first floor makes `new_lossFactor` too large (surviving fraction too small).

`postSlashCredit` (`src/Midnight.sol:805-806`): [2](#0-1) 

For a lender synced before the event (`_lastLossFactor = old_lossFactor`):
`postSlashCredit_i = ⌊c_i · ⌊A·(T−B)/T⌋ / A⌋`

This is a double floor. The `mulDivAddDownDown` Certora rule (`certora/specs/MulDiv.spec:30-33`) formally proves `Σ mulDivDown(c_i, b, d) ≤ mulDivDown(Σc_i, b, d)`, confirming the aggregate credit loss exceeds the actual bad debt by up to N−1 units. [3](#0-2) 

**Concrete minimal example:**
- 2 lenders, `credit_1 = credit_2 = 1`; `totalUnits = 2`, `badDebt = 1`, `old_lossFactor = 0`
- `new_lossFactor = MAX − ⌊MAX/2⌋ = MAX − (2^127−1) = 2^127`
- Each lender: `postSlashCredit = ⌊1·(2^127−1)/(2^128−1)⌋ = 0`
- `totalUnits` after bad debt = 1, `Σ credit_i = 0` → 1 unit permanently orphaned

**Why existing checks fail:**

`totalUnitsEqualsSumNegativeDebtPlusWithdrawable` (`certora/specs/Midnight.spec:123-124`) tracks `totalUnits = sumDebt + withdrawable` but does not track `Σ credit_i`, so the orphaned-unit discrepancy is invisible to all formal specs. [4](#0-3) 

`liquidateEffects` (`certora/specs/BalanceEffects.spec:182`) asserts `creditOf(anyId, anyUser) == otherCreditBefore`, but `creditOf` returns the raw stored `position[id][user].credit` which is only updated by `updatePosition`, not by `liquidate` itself — so this rule never observes the post-slash aggregate. [5](#0-4) 

`LossFactor.spec` rules (`certora/specs/LossFactor.spec:63-70`) verify per-lender sync after `updatePosition`, not the aggregate sum across all lenders. [6](#0-5) 

Test tolerance `assertApproxEqAbs(..., 1)` (`test/LiquidationTest.sol:361`) covers single-lender rounding only; no test exercises the multi-lender aggregate sum. [7](#0-6) 

## Impact Explanation
Lenders collectively lose up to N−1 extra units per bad-debt event beyond their proportional share of the actual bad debt. These units remain counted in `totalUnits` (and flow into `withdrawable` after borrower repayment) but no lender holds credit to claim them, making them permanently locked in the contract. Over K bad-debt events with N lenders each, up to (N−1)·K units are irreversibly orphaned. For lenders with minimum-unit positions (credit = 1), the rounding can eliminate 100% of their remaining post-slash credit. This constitutes permanent, irreversible value leakage from lenders and violates the accounting invariant `Σ credit_losses = total_bad_debt_realized`.

## Likelihood Explanation
Triggered by any bad-debt liquidation in a market with more than one lender — the standard operating condition for any active market. No privileged access, no oracle manipulation, and no special sequencing is required beyond a normal `liquidate` call. The effect accumulates monotonically over the market lifetime and is amplified by markets with many lenders or frequent bad-debt events.

## Recommendation
Replace the per-lender `mulDivDown` in `updatePositionView` with `mulDivUp` for `postSlashCredit`, rounding in favor of lenders rather than against them. This bounds the aggregate over-credit to at most N−1 units (instead of orphaning N−1 units), which is the standard DeFi convention for share-accounting rounding. Alternatively, track a protocol-owned "dust" credit that absorbs rounding remainders so that `Σ credit_i + dust_credit = totalUnits − sumDebt` holds exactly at all times.

## Proof of Concept
Deploy a market with two lenders each supplying 1 unit (`credit_1 = credit_2 = 1`, `totalUnits = 2`). Trigger a bad-debt liquidation with `badDebt = 1`. Call `updatePosition` for both lenders. Assert:
- `creditOf(id, lender1) == 0`
- `creditOf(id, lender2) == 0`
- `totalUnits(id) == 1`
- `withdrawable(id) == 1`

The 1 unit in `withdrawable` is unclaimable by either lender, confirming permanent orphaning. This is a direct extension of the existing single-lender test at `test/LiquidationTest.sol:361` with a second lender added and the tolerance changed from `assertApproxEqAbs(..., 1)` to an exact equality check on the aggregate. [8](#0-7)

### Citations

**File:** src/Midnight.sol (L631-633)
```text
            _marketState.lossFactor = UtilsLib.toUint128(
                type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
            );
```

**File:** src/Midnight.sol (L805-807)
```text
        uint256 postSlashCredit = _lastLossFactor < type(uint128).max
            ? credit.mulDivDown(type(uint128).max - marketState[id].lossFactor, type(uint128).max - _lastLossFactor)
            : 0;
```

**File:** certora/specs/MulDiv.spec (L30-33)
```text
rule mulDivAddDownDown(uint256 a1, uint256 a2, uint256 b, uint256 d) {
    uint256 a1plusa2 = require_uint256(a1 + a2);
    assert mulDivDown(a1, b, d) + mulDivDown(a2, b, d) <= mulDivDown(a1plusa2, b, d);
}
```

**File:** certora/specs/Midnight.spec (L123-124)
```text
strong invariant totalUnitsEqualsSumNegativeDebtPlusWithdrawable(bytes32 id)
    to_mathint(totalUnits(id)) == sumDebt[id] + to_mathint(withdrawable(id));
```

**File:** certora/specs/BalanceEffects.spec (L182-182)
```text
    assert creditOf(anyId, anyUser) == otherCreditBefore;
```

**File:** certora/specs/LossFactor.spec (L63-70)
```text
/// After updatePosition, the user's lastLossFactor is synced to the market's lossFactor.
rule updatePositionSyncsLastLossFactor(env e, Midnight.Market market, address user) {
    bytes32 id = summaryToId(market);

    updatePosition(e, market, user);

    assert lastLossFactor(id, user) == currentContract.marketState[id].lossFactor;
}
```

**File:** test/LiquidationTest.sol (L355-362)
```text
        midnight.liquidate(market, 0, 0, 0, borrower, false, address(this), address(0), "");

        assertEq(midnight.debtOf(id, borrower), units - expectedBadDebt, "debt");
        assertEq(midnight.totalUnits(id), units - expectedBadDebt, "total units");
        assertEq(midnight.creditOf(id, lender), units, "lender units");
        midnight.updatePosition(market, lender);
        assertApproxEqAbs(midnight.creditOf(id, lender), units - expectedBadDebt, 1, "lender units after slashing");
    }
```
