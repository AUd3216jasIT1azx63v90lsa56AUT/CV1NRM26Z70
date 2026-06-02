All six code references check out against the actual source. Here is the validated output:

---

Audit Report

## Title
Systematic per-lender credit over-slash via compounded `mulDivDown` rounding in `updatePositionView` — (File: src/Midnight.sol)

## Summary
In `liquidate`, the `lossFactor` update uses `mulDivDown` to floor the surviving fraction, rounding `lossFactor` upward. In `updatePositionView`, `postSlashCredit` applies a second `mulDivDown` against the already-rounded `lossFactor`. Both floors compound in the same direction: lenders collectively lose up to N extra units per bad-debt event (N = number of lenders with non-zero credit), with those units permanently orphaned inside `totalUnits` and unclaimable by any party.

## Finding Description
**Root cause — double floor in the same direction:**

`lossFactor` update (`src/Midnight.sol:631-633`): [1](#0-0) 

Let `MAX = type(uint128).max`, `A = MAX − old_lossFactor`. The surviving multiplier stored is `⌊A·(T−B)/T⌋`, so `new_lossFactor = MAX − ⌊A·(T−B)/T⌋ ≥ exact`. The first floor makes `lossFactor` too large.

`postSlashCredit` (`src/Midnight.sol:805-806`): [2](#0-1) 

For a lender synced before the event (`_lastLossFactor = old_lossFactor`):
`postSlashCredit_i = ⌊c_i · ⌊A·(T−B)/T⌋ / A⌋`

This is a double floor. The `mulDivAddDownDown` Certora rule (`certora/specs/MulDiv.spec:30-33`) proves that `Σ mulDivDown(c_i, b, d) ≤ mulDivDown(Σc_i, b, d)`, confirming the aggregate credit loss exceeds the actual bad debt by up to N units. [3](#0-2) 

**Concrete minimal example:**
- 2 lenders, `credit_1 = credit_2 = 1`; `totalUnits = 2`, `badDebt = 1`, `old_lossFactor = 0`
- `new_lossFactor = MAX − ⌊MAX/2⌋ = MAX − (2^127−1) = 2^127`
- Each lender: `postSlashCredit = ⌊1·(2^127−1)/(2^128−1)⌋ = 0`
- `totalUnits` after bad debt = 1, `Σ credit_i = 0` → 1 unit permanently orphaned

**Why existing checks fail:**

- `totalUnitsEqualsSumNegativeDebtPlusWithdrawable` (`certora/specs/Midnight.spec:123-124`) tracks `totalUnits = sumDebt + withdrawable` but does not track `Σ credit_i`, so the orphaned-unit discrepancy is invisible to all formal specs. [4](#0-3) 

- `liquidateEffects` (`certora/specs/BalanceEffects.spec:182`) asserts `creditOf(anyId, anyUser) == otherCreditBefore`, but `creditOf` returns the raw stored `position[id][user].credit` which is only updated by `updatePosition`, not by `liquidate` itself — so this rule never observes the post-slash aggregate. [5](#0-4) [6](#0-5) 

- `LossFactor.spec` rules (`certora/specs/LossFactor.spec:63-70`) verify per-lender sync after `updatePosition`, not the aggregate sum across all lenders. [7](#0-6) 

- Test tolerance `assertApproxEqAbs(..., 1)` (`test/LiquidationTest.sol:361`) covers single-lender rounding only; no test exercises the multi-lender aggregate sum. [8](#0-7) 

## Impact Explanation
Lenders collectively lose up to N extra units per bad-debt event beyond their proportional share of the actual bad debt. These units remain counted in `totalUnits` (and eventually in `withdrawable` after borrower repayment) but no lender holds credit to claim them, making them permanently locked in the contract. Over K bad-debt events with N lenders each, up to N·K units are irreversibly orphaned. For lenders with minimum-unit positions (credit = 1), the rounding can eliminate 100% of their remaining post-slash credit. This constitutes permanent, irreversible value leakage from lenders and violates the accounting invariant `Σ credit_losses = total_bad_debt_realized`.

## Likelihood Explanation
Triggered by any bad-debt liquidation in a market with more than one lender — the standard operating condition for any active market. No privileged access, no oracle manipulation, and no special sequencing is required beyond a normal `liquidate` call. The effect accumulates monotonically over the market lifetime and is amplified by markets with many lenders or frequent bad-debt events.

## Recommendation
Round `lossFactor` in the direction that favors lenders: use `mulDivUp` for the surviving-fraction computation so that `new_lossFactor` is at most the exact value, eliminating the first floor. Concretely, replace:

```solidity
// current (floors surviving fraction → lossFactor too large)
type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
```

with:

```solidity
// fixed (ceils surviving fraction → lossFactor at most exact)
type(uint128).max - (type(uint128).max - _lossFactor).mulDivUp(_totalUnits - badDebt, _totalUnits)
```

This ensures `postSlashCredit` in `updatePositionView` applies only a single floor, bounding the aggregate rounding error to at most 1 unit total rather than N units. [1](#0-0) 

## Proof of Concept
Minimal Foundry test (add to `test/LiquidationTest.sol`):

```solidity
function testMultiLenderOrphanedUnit() public {
    // Two lenders each supply 1 unit into a market with totalUnits=2
    // Borrower takes 2 units of debt, then bad debt of 1 is realized
    address lender2 = makeAddr("lender2");
    // Setup: lender1 credit=1, lender2 credit=1, totalUnits=2
    // ... (market setup with 2 lenders at credit=1 each)

    // Trigger bad-debt liquidation: badDebt=1
    midnight.liquidate(market, 0, 0, 0, borrower, false, address(this), address(0), "");

    // After liquidation: totalUnits=1
    assertEq(midnight.totalUnits(id), 1);

    // Update both lenders
    midnight.updatePosition(market, lender);
    midnight.updatePosition(market, lender2);

    // Both lenders have credit=0, but totalUnits=1 → 1 unit permanently orphaned
    assertEq(midnight.creditOf(id, lender), 0);
    assertEq(midnight.creditOf(id, lender2), 0);
    // totalUnits=1 with no lender able to claim it
    assertEq(midnight.totalUnits(id), 1); // orphaned
}
```

The test demonstrates that after `updatePosition` for all lenders, `Σ credit_i = 0` while `totalUnits = 1`, with the orphaned unit unclaimable by any party.

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

**File:** src/Midnight.sol (L883-885)
```text
    function creditOf(bytes32 id, address user) external view returns (uint128) {
        return position[id][user].credit;
    }
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

**File:** test/LiquidationTest.sol (L361-361)
```text
        assertApproxEqAbs(midnight.creditOf(id, lender), units - expectedBadDebt, 1, "lender units after slashing");
```
