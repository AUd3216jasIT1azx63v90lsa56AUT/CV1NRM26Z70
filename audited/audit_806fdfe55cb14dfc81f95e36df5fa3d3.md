Audit Report

## Title
Missing `repaidUnits` cap in post-maturity `seizedAssets` liquidation path causes arithmetic underflow revert - (File: `src/Midnight.sol`)

## Summary
In `liquidate`, when `seizedAssets > 0` and `postMaturityMode = true`, `repaidUnits` is computed via two `mulDivUp` calls with no upper bound applied against `_position.debt`. The `maxRepaid` guard that prevents over-repayment in normal mode is entirely absent from the post-maturity branch. When the oracle price exceeds `ORACLE_PRICE_SCALE * _position.debt / seizedAssets`, the computed `repaidUnits` exceeds the borrower's actual debt and the Solidity 0.8 checked subtraction at line 676 reverts, permanently blocking the `seizedAssets` liquidation path for that position.

## Finding Description

**Root cause:** In `src/Midnight.sol`, the `seizedAssets` branch computes `repaidUnits` via two rounding-up multiplications: [1](#0-0) 

No cap of `repaidUnits` against `_position.debt` follows. The only guard that could prevent `repaidUnits > _position.debt` is the `maxRepaid` block, but it is gated on `!postMaturityMode`: [2](#0-1) 

In post-maturity mode that entire block is skipped. The uncapped `repaidUnits` then reaches the checked subtraction: [3](#0-2) 

**Why the `badDebt` block does not help:** The `badDebt` loop subtracts `mulDivUp(collateral, price, ORACLE_PRICE_SCALE).mulDivUp(WAD, maxLif)` from `originalDebt`. With a high price (e.g., `2 * ORACLE_PRICE_SCALE`) and `collateral = 1`, this value equals 2, which exceeds `debt = 1`, so `zeroFloorSub` yields `badDebt = 0`. `_position.debt` is therefore unchanged before line 676. [4](#0-3) 

**Concrete arithmetic:** With `seizedAssets = 1`, `liquidatedCollatPrice = 2 * ORACLE_PRICE_SCALE`, `lif = WAD`:
- `mulDivUp(1, 2e36, 1e36) = 2`
- `mulDivUp(2, WAD, WAD) = 2` → `repaidUnits = 2`

If `_position.debt = 1`, then `uint128(1) - uint128(2)` reverts under checked arithmetic.

**Liquidatability check passes unconditionally in post-maturity mode** — only `block.timestamp > market.maturity` is required: [5](#0-4) 

**Existing test coverage misses this:** `testLiquidateCollateralInput` bounds `liquidationOraclePrice` to `<= ORACLE_PRICE_SCALE`, explicitly excluding the triggering price range: [6](#0-5) 

`testLiquidatePostMaturityPartialLIF` uses the `repaidUnits` input path (`seizedAssets = 0`), not the `seizedAssets` path: [7](#0-6) 

## Impact Explanation
Any liquidator calling `liquidate` with `seizedAssets > 0` in post-maturity mode on a position where `liquidatedCollatPrice > ORACLE_PRICE_SCALE * _position.debt / seizedAssets` receives an unconditional arithmetic underflow revert. The `seizedAssets` liquidation path is permanently blocked for that position under those oracle conditions. The `repaidUnits` path is economically irrational as a substitute when the price is high (it rounds down to `seizedAssets = 0`). This constitutes an unexpected revert of a core protocol function (liquidation) for a reachable and repeatable set of inputs.

## Likelihood Explanation
All four preconditions are independently reachable by unprivileged users without any privileged action:
1. **Post-maturity** — normal market lifecycle.
2. **Small borrower debt** — any borrower who partially repaid or was partially liquidated can have `debt = 1`.
3. **Oracle price > `ORACLE_PRICE_SCALE`** — explicitly used in the existing test suite (e.g., `testLiquidatePostMaturityPartialLIF` uses up to `10 * ORACLE_PRICE_SCALE`); occurs naturally for high-value collateral assets.
4. **Borrower has ≥ `seizedAssets` collateral** — required to pass the collateral subtraction at line 670.

The condition is repeatable: every call with `seizedAssets = 1` under these parameters reverts.

## Recommendation
After computing `repaidUnits` in the `seizedAssets` branch, cap it against the remaining debt:

```solidity
if (seizedAssets > 0) {
    repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
    repaidUnits = UtilsLib.min(repaidUnits, _position.debt); // cap to avoid underflow
}
```

Alternatively, apply a single cap immediately before line 676 that covers both branches in post-maturity mode:

```solidity
if (postMaturityMode) {
    repaidUnits = UtilsLib.min(repaidUnits, _position.debt);
}
```

## Proof of Concept
Minimal Foundry test (add to `LiquidationTest.sol`):

```solidity
function testPostMaturitySeizedAssetsUnderflow() public {
    uint256 units = 1;
    collateralize(market, borrower, units);
    setupMarket(market, units);

    // Price > ORACLE_PRICE_SCALE * debt / seizedAssets = 1e36 * 1 / 1 = 1e36
    Oracle(market.collateralParams[0].oracle).setPrice(2 * ORACLE_PRICE_SCALE);
    vm.warp(market.maturity + TIME_TO_MAX_LIF);

    // seizedAssets = 1, repaidUnits input = 0 → seizedAssets path
    // repaidUnits = mulDivUp(1, 2e36, 1e36).mulDivUp(WAD, WAD) = 2
    // _position.debt = 1 → underflow revert
    vm.expectRevert(); // arithmetic underflow
    midnight.liquidate(market, 0, 1, 0, borrower, true, address(this), address(0), "");
}
```

### Citations

**File:** src/Midnight.sol (L614-616)
```text
            badDebt = badDebt.zeroFloorSub(
                _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
            );
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/Midnight.sol (L649-650)
```text
            if (seizedAssets > 0) {
                repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
```

**File:** src/Midnight.sol (L655-668)
```text
            if (!postMaturityMode) {
                uint256 lltv = market.collateralParams[collateralIndex].lltv;
                // Note that debt >= maxDebt in this branch.
                // The imprecision in this computation is at most a few hundreds collateral or loan token assets.
                uint256 maxRepaid = lltv < WAD
                    ? (_position.debt - maxDebt).mulDivUp(WAD * WAD, WAD * WAD - lif * lltv)
                    : type(uint256).max;
                require(
                    repaidUnits <= maxRepaid
                        || _position.collateral[collateralIndex].mulDivDown(liquidatedCollatPrice, ORACLE_PRICE_SCALE)
                            .mulDivDown(WAD, lif).zeroFloorSub(maxRepaid) < market.rcfThreshold,
                    RecoveryCloseFactorConditionsViolated()
                );
            }
```

**File:** src/Midnight.sol (L676-676)
```text
            _position.debt -= UtilsLib.toUint128(repaidUnits);
```

**File:** test/LiquidationTest.sol (L220-220)
```text
        liquidationOraclePrice = bound(liquidationOraclePrice, badDebtPriceDown(units) + 1, ORACLE_PRICE_SCALE);
```

**File:** test/LiquidationTest.sol (L534-534)
```text
        midnight.liquidate(market, 0, 0, repaid, borrower, true, address(this), address(0), "");
```
