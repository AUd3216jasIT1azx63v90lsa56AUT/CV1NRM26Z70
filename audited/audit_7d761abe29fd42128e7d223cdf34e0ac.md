Audit Report

## Title
Double `mulDivDown` Truncation in `maxDebt` Computation Allows Liquidation of Truly Healthy Positions - (File: src/Midnight.sol)

## Summary

The `maxDebt` computation in `liquidate()` and `isHealthy()` chains two `mulDivDown` calls — first dividing by `ORACLE_PRICE_SCALE` (1e36), then by `WAD` (1e18). Each division independently truncates a fractional remainder. When both remainders are large enough, their combined loss exceeds 1 unit, causing `computed_maxDebt` to be exactly 1 less than `⌊true_maxDebt⌋`. A position where `debt == ⌊true_maxDebt⌋` is mathematically solvent, yet the protocol computes `maxDebt = debt − 1`, satisfying `originalDebt > maxDebt` and allowing an unprivileged liquidator to seize collateral from a healthy borrower.

## Finding Description

**Exact code path:**

`src/Midnight.sol` line 613 (`liquidate()`):
```solidity
maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(_collateralParam.lltv, WAD);
```

`src/Midnight.sol` lines 954–955 (`isHealthy()`):
```solidity
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
    .mulDivDown(collateralParam.lltv, WAD);
```

`src/libraries/UtilsLib.sol` line 29–31:
```solidity
function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y) / d;
}
```

**Root cause — double truncation:**

Let `step1 = ⌊collateral × price / ORACLE_PRICE_SCALE⌋` with remainder `r1 = (collateral × price) mod ORACLE_PRICE_SCALE`. Then `computed_maxDebt = ⌊step1 × lltv / WAD⌋` with remainder `r2 = (step1 × lltv) mod WAD`.

The true maxDebt is:
```
true_maxDebt = computed_maxDebt + r2/WAD + r1×lltv/(ORACLE_PRICE_SCALE×WAD)
```

The discrepancy reaches ≥ 1 when:
```
r2 × ORACLE_PRICE_SCALE + r1 × lltv  ≥  ORACLE_PRICE_SCALE × WAD
```

**Concrete example with `LLTV_0 = 0.385e18` (confirmed allowed tier in `src/libraries/ConstantsLib.sol` line 29):**

| Parameter | Value |
|---|---|
| `collateral` | `187020000000000000000` |
| `price` | `1000000000000000000` (1e18) |
| `lltv` | `385000000000000000` (0.385e18) |
| `debt` | `72` |

- `step1 = ⌊187020000000000000000 × 10^18 / 10^36⌋ = 187`, `r1 = 2×10^34`
- `step1 × lltv = 187 × 385×10^15 = 71995×10^15`
- `r2 = 71995×10^15 mod 10^18 = 995×10^15`
- `computed_maxDebt = ⌊71995×10^15 / 10^18⌋ = 71`
- `true_maxDebt = 187.02 × 0.385 = 72.0027`

Position with `debt = 72` is **truly healthy** (72.0027 ≥ 72), but `computed_maxDebt = 71`.

**Liquidatability check at `src/Midnight.sol` lines 620–624:**
```solidity
require(
    !liquidationLocked(id, borrower)
        && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
    NotLiquidatable()
);
```

`originalDebt (72) > maxDebt (71)` → `true` → `NotLiquidatable` is **not** thrown → liquidation proceeds.

**Exploit path:**
1. Borrower supplies `187020000000000000000` collateral tokens and borrows 72 units at a price where `computed_maxDebt ≥ 72` (e.g., price slightly above 1e18). The `isHealthy` check at borrow time passes.
2. Oracle price moves naturally to exactly 1e18 (no manipulation required).
3. At this price, `true_maxDebt = 72.0027 ≥ 72` — the position is genuinely solvent.
4. An unprivileged liquidator calls `liquidate()`. The protocol computes `maxDebt = 71`, satisfies `72 > 71`, and proceeds to seize collateral.

**Why existing checks fail:**

The `Healthiness.spec` Certora spec (`certora/specs/Healthiness.spec` lines 23–24) replaces `mulDivDown` with a ghost summary `summaryMulDivDown` axiomatized only for monotonicity and zero-input behavior. It does not model the concrete integer truncation of the chained division, so the 1-unit underestimation is invisible to the prover. The `stayHealthyLiquidateSameBorrower` rule requires `callIsHealthy()` to return `true` before the call using the ghost summary — but the ghost summary can satisfy this precondition for inputs where the concrete `isHealthy()` returns `false`, meaning the spec does not cover this rounding boundary.

## Impact Explanation

An unprivileged liquidator can call `liquidate()` on a position that is mathematically solvent (true collateral value × LLTV ≥ debt), seizing the borrower's collateral and forcing repayment of debt the borrower was not obligated to repay. This constitutes unauthorized theft of collateral from a solvent borrower. The impact is direct asset loss to the borrower with no recourse.

## Likelihood Explanation

The condition `r2 × ORACLE_PRICE_SCALE + r1 × lltv ≥ ORACLE_PRICE_SCALE × WAD` is satisfiable at every allowed LLTV tier. Oracle prices are continuous and change over time; for any given borrower position, there exist price values at which the rounding boundary is crossed. A liquidation bot can monitor for these conditions systematically. The precondition requires no privileged access, no oracle manipulation, and no user error — only a specific oracle price value that the market reaches naturally.

## Recommendation

Replace the chained `mulDivDown` with a single combined operation that avoids intermediate truncation:

```solidity
maxDebt += _collateral.mulDivDown(price * _collateralParam.lltv, ORACLE_PRICE_SCALE * WAD);
```

This computes `⌊collateral × price × lltv / (ORACLE_PRICE_SCALE × WAD)⌋` in a single division, eliminating the intermediate truncation. Overflow safety must be verified: `price * lltv` fits in uint256 since `price ≤ ORACLE_PRICE_SCALE` (bounded by the overflow spec) and `lltv ≤ WAD`, giving `price * lltv ≤ ORACLE_PRICE_SCALE * WAD = 10^54`, which fits in uint256. The same fix must be applied identically in both `liquidate()` and `isHealthy()` to keep them consistent.

## Proof of Concept

Minimal Foundry test (add to `test/LiquidationTest.sol`):

```solidity
function testLiquidateHealthyPositionDoubleTruncation() public {
    // Use LLTV_0 = 0.385e18
    // collateral = 187020000000000000000, price = 1e18, debt = 72
    // true_maxDebt = 72.0027, computed_maxDebt = 71

    uint256 collateralAmount = 187020000000000000000;
    uint256 oraclePrice = 1e18; // ORACLE_PRICE_SCALE = 1e36, so price = 1e18

    // Verify the double truncation manually
    uint256 step1 = collateralAmount * oraclePrice / ORACLE_PRICE_SCALE; // = 187
    uint256 computedMaxDebt = step1 * LLTV_0 / WAD; // = 71
    assertEq(computedMaxDebt, 71);

    // Set up market with LLTV_0
    // Supply collateralAmount, borrow 72 units at a price where computed_maxDebt >= 72
    // (e.g., price = 1.01e18 gives step1=188, maxDebt=72)
    oracle1.setPrice(1.01e18);
    // ... supply collateral, borrow 72 units ...

    // Drop price to 1e18 — position is truly healthy (72.0027 >= 72)
    oracle1.setPrice(oraclePrice);

    // Verify isHealthy returns false (the bug)
    assertFalse(midnight.isHealthy(market, id, borrower));

    // Liquidation succeeds on a healthy position
    midnight.liquidate(market, 0, 0, 1, borrower, false, address(this), address(0), "");
}
```