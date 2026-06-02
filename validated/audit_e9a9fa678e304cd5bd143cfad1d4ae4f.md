All code references check out. Let me verify the key claims:

**`mulDivDown`** at `src/libraries/UtilsLib.sol:29-31` is confirmed as plain integer floor division. [1](#0-0) 

**`isHealthy`** at `src/Midnight.sol:954-955` confirmed two-step pattern. [2](#0-1) 

**`liquidate`** at `src/Midnight.sol:613` confirmed identical two-step pattern. [3](#0-2) 

**Liquidation gate** at lines 620-624 confirmed: fires on `originalDebt > maxDebt`. [4](#0-3) 

**Constants** confirmed: `WAD = 1e18`, `ORACLE_PRICE_SCALE = 1e36`, `LLTV_7 = 0.98e18`. [5](#0-4) 

**Comment at line 116** documents rounding direction but not the two-step composition error. [6](#0-5) 

**Math verified**: With `C=1`, `P=51e36+2.1e34`, `L=0.98e18`: two-step gives `floor(51*0.98)=49`; single-step gives `floor(50.00058)=50`. Discrepancy of exactly 1 confirmed.

**`liquidationLocked`** is transient storage set only during `take` callbacks — a standalone `liquidate` call has it as `false`. [7](#0-6) 

No Security.md or Researcher.md found to exclude this class of issue.

---

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

Replace the two sequential `mulDivDown` calls with a single combined floor division in both `isHealthy` and `liquidate`:

```solidity
// Instead of:
maxDebt += collateral.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(lltv, WAD);

// Use:
maxDebt += collateral.mulDivDown(price * lltv, ORACLE_PRICE_SCALE * WAD);
```

This computes `floor(collateral * price * lltv / (ORACLE_PRICE_SCALE * WAD))` in a single step, eliminating the intermediate truncation. Care must be taken to ensure `price * lltv` does not overflow; given `price` is oracle-supplied and `lltv <= WAD = 1e18`, the product fits in 256 bits for any realistic oracle price up to ~`1.16e59`.

## Proof of Concept

```solidity
// Minimal Foundry unit test
function test_healthyPositionLiquidatable() public {
    // Setup: market with LLTV_7 = 0.98e18, single collateral
    // 1. Supply C=1 collateral unit
    // 2. Set oracle price to 52e36, borrow 50 debt units (passes isHealthy: floor(52*0.98)=50)
    // 3. Update oracle price to 51e36 + 2.1e34
    // 4. Assert isHealthy returns false (two-step: maxDebt=49 < debt=50)
    // 5. Assert single-step floor(C*P*L/(S*W)) = 50 >= debt (position truly healthy)
    // 6. Call liquidate() — confirm it does not revert with NotLiquidatable
    // 7. Confirm borrower's collateral was seized despite being solvent
}
```

The fuzz invariant to catch this class of bug: for all `(C, P, L)`, assert that if `C.mulDivDown(P*L, ORACLE_PRICE_SCALE*WAD) >= debt` then `liquidate()` reverts with `NotLiquidatable`.

### Citations

**File:** src/libraries/UtilsLib.sol (L28-31)
```text
    /// @dev Returns (x * y) / d rounded down.
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** src/Midnight.sol (L116-116)
```text
/// @dev maxDebt is rounded down in isHealthy and liquidate.
```

**File:** src/Midnight.sol (L613-613)
```text
            maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(_collateralParam.lltv, WAD);
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/Midnight.sol (L953-955)
```text
                uint256 price = IOracle(collateralParam.oracle).price();
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
```

**File:** src/libraries/ConstantsLib.sol (L8-37)
```text
uint256 constant WAD = 1e18;
uint256 constant ORACLE_PRICE_SCALE = 1e36;
uint256 constant CBP = 1e12;
uint256 constant MAX_SETTLEMENT_FEE_0_DAYS = 0.000014e18;
uint256 constant MAX_SETTLEMENT_FEE_1_DAY = 0.000014e18;
uint256 constant MAX_SETTLEMENT_FEE_7_DAYS = 0.000098e18;
uint256 constant MAX_SETTLEMENT_FEE_30_DAYS = 0.000417e18;
uint256 constant MAX_SETTLEMENT_FEE_90_DAYS = 0.00125e18;
uint256 constant MAX_SETTLEMENT_FEE_180_DAYS = 0.0025e18;
uint256 constant MAX_SETTLEMENT_FEE_360_DAYS = 0.005e18;
uint32 constant MAX_CONTINUOUS_FEE = uint32(uint256(0.01e18) / uint256(365 days));
uint256 constant TIME_TO_MAX_LIF = 15 minutes;
uint256 constant MAX_COLLATERALS = 128;
uint256 constant MAX_COLLATERALS_PER_BORROWER = 16;
uint256 constant LIQUIDATION_CURSOR_LOW = 0.25e18;
uint256 constant LIQUIDATION_CURSOR_HIGH = 0.5e18;
uint256 constant LIQUIDATION_LOCK_SLOT = uint256(keccak256("morpho.midnight.liquidationLocked"));
bytes32 constant CALLBACK_SUCCESS = keccak256("morpho.midnight.callbackSuccess");
uint8 constant DEFAULT_TICK_SPACING = 4;

/// @dev The allowed LLTV values, copied from Morpho Blue's enabled tiers (excluding zero, including WAD).
uint256 constant LLTV_0 = 0.385e18;
uint256 constant LLTV_1 = 0.625e18;
uint256 constant LLTV_2 = 0.77e18;
uint256 constant LLTV_3 = 0.86e18;
uint256 constant LLTV_4 = 0.915e18;
uint256 constant LLTV_5 = 0.945e18;
uint256 constant LLTV_6 = 0.965e18;
uint256 constant LLTV_7 = 0.98e18;
uint256 constant LLTV_8 = 1e18;
```
