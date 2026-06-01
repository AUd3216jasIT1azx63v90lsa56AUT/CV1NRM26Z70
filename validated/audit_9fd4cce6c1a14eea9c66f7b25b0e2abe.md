Looking at the actual code to validate each claim in the report.

Audit Report

## Title
Two-step `mulDivDown` rounding in health check allows liquidation of truly-healthy positions - (File: src/Midnight.sol)

## Summary
Both `isHealthy` and `liquidate` compute maximum debt capacity via two sequential floor divisions: `collateral.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(lltv, WAD)`. This two-step floor can produce a result exactly 1 unit less than the mathematically correct single-step `floor(collateral * price * lltv / (ORACLE_PRICE_SCALE * WAD))`. When the oracle price lands in the narrow band where this discrepancy occurs and the borrower's debt equals the single-step result, the position is genuinely solvent but the protocol treats it as liquidatable, allowing an unprivileged liquidator to seize collateral amplified by the LIF.

## Finding Description

**Exact code path:**

`isHealthy` at `src/Midnight.sol` lines 954–955:
```solidity
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
    .mulDivDown(collateralParam.lltv, WAD);
```

`liquidate` at `src/Midnight.sol` line 613:
```solidity
maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(_collateralParam.lltv, WAD);
```

`mulDivDown` at `src/libraries/UtilsLib.sol` lines 29–31 is a plain integer floor:
```solidity
function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y) / d;
}
```

The liquidation gate at `src/Midnight.sol` lines 620–624 fires when `originalDebt > maxDebt`.

**Root cause — two-step floor loses up to 1 unit:**

Let `A = C * P`, `q = floor(A / S)`, `r = A mod S` (with `S = ORACLE_PRICE_SCALE = 1e36`, `W = WAD = 1e18`, `L = lltv`).

- Two-step result: `floor(q * L / W)`
- Single-step result: `floor(A * L / (S * W)) = floor(q*L/W + r*L/(S*W))`

The difference equals 1 exactly when `{q*L/W} + r*L/(S*W) >= 1`. Since `r < S` and `L <= W`, the term `r*L/(S*W) < 1`, so the maximum discrepancy is **exactly 1**.

**Concrete witness (LLTV_7 = 0.98e18):**

Choose `C = 1`, `L = 0.98e18`, `P = 51e36 + 2.1e34`:
- `q = floor((51e36 + 2.1e34) / 1e36) = 51`
- Two-step: `floor(51 * 0.98) = floor(49.98) = 49`
- Single-step: `floor((51e36 + 2.1e34) * 0.98 / 1e36) = floor(50.00058) = 50`

If `debt = 50`: two-step gives `maxDebt = 49`, so `originalDebt (50) > maxDebt (49)` → liquidation proceeds. Single-step gives `maxDebt = 50 >= debt` → position is truly healthy.

**Exploit flow:**

1. Borrower supplies `C = 1` unit of collateral.
2. At prior oracle price `P' = 52e36`, both formulas give `maxDebt = floor(52 * 0.98) = 50`. Borrower borrows 50 units. The `take` health check at line 476 passes.
3. Oracle moves passively to `P = 51e36 + 2.1e34` (no manipulation required).
4. Liquidator calls `liquidate()`. The two-step formula gives `maxDebt = 49`. The `NotLiquidatable` guard evaluates `50 > 49 = true` and does not revert.
5. Liquidator repays debt and seizes collateral amplified by `lif > WAD`.

**Why existing checks fail:**

- `liquidationLocked` (transient storage, set during `take` callbacks) is not active in a standalone `liquidate` call.
- The `NotLiquidatable` guard at lines 620–624 uses the same two-step `maxDebt`, so it confirms the (incorrect) liquidatability.
- The protocol comment at line 116 ("maxDebt is rounded down in isHealthy and liquidate") documents the rounding direction but does not acknowledge or justify the additional 1-unit error introduced by the two-step composition versus a single-step floor.

## Impact Explanation

A liquidator can call `liquidate()` on a position that is genuinely solvent at the current oracle price. The borrower loses collateral at a rate amplified by `lif` (which exceeds `WAD` for all non-`LLTV_8` tiers), receiving less than fair value in return. This directly violates the core protocol invariant that healthy positions are not liquidatable, constituting unauthorized movement of user assets.

## Likelihood Explanation

The vulnerable price band has width approximately `S * W / (L * C)` price units. For `C = 1` and `LLTV_7`, this is roughly `1e36 * 1e18 / (0.98e18 * 1) ≈ 1.02e36` — a wide band relative to oracle granularity. For larger collateral amounts the band narrows proportionally, but the condition remains passively observable without any oracle manipulation. The vulnerability applies to all eight non-`LLTV_8` tiers (`LLTV_0` through `LLTV_7`). Any liquidator monitoring oracle prices can trigger this the moment the price enters the band, and the condition is repeatable across any market using a non-WAD LLTV.

## Recommendation

Replace the two sequential `mulDivDown` calls with a single-step computation that avoids the intermediate floor. Since `_collateral * price * lltv` can overflow `uint256`, use the following decomposition which stays within bounds:

```solidity
uint256 A = _collateral * price;                  // safe: collateral <= uint128, price <= 1e36
uint256 q = A / ORACLE_PRICE_SCALE;
uint256 r = A % ORACLE_PRICE_SCALE;
uint256 qL = q * lltv;                            // safe: q <= uint128, lltv <= WAD = 1e18
uint256 a = qL / WAD;
uint256 b = qL % WAD;
// b * ORACLE_PRICE_SCALE + r * lltv < 2e54, well within uint256
maxDebt += a + (b * ORACLE_PRICE_SCALE + r * lltv) / (ORACLE_PRICE_SCALE * WAD);
```

This computes `floor(collateral * price * lltv / (ORACLE_PRICE_SCALE * WAD))` exactly, eliminating the 1-unit discrepancy. Apply the same fix symmetrically in both `isHealthy` and `liquidate`.

## Proof of Concept

Minimal Foundry unit test outline:

```solidity
// Setup: market with LLTV_7 = 0.98e18, single collateral, C = 1 token unit
// Oracle returns P' = 52e36 initially

// Step 1: borrower supplies 1 unit of collateral
// Step 2: borrower borrows 50 units via take() — passes health check (maxDebt = 50)

// Step 3: set oracle to return P = 51e36 + 2.1e34

// Step 4: assert isHealthy() returns false  (two-step: maxDebt = 49 < 50)
// Step 5: call liquidate(collateralIndex, 0, 50, borrower, false, ...)
//         assert it does NOT revert (NotLiquidatable guard passes: 50 > 49)
// Step 6: assert borrower's collateral was seized despite being solvent
//         by single-step formula (floor(50.00058) = 50 >= debt 50)
```

Expected result: liquidation succeeds and borrower loses collateral, confirming the invariant violation.