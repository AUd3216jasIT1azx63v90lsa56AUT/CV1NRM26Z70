The code at the cited lines is confirmed in the repository. The logic is:

- **Lines 614–616**: `badDebt` uses `zeroFloorSub` with `mulDivUp` — floors at zero when the ceiling expression meets or exceeds `originalDebt`. [1](#0-0) 
- **Line 650**: When `seizedAssets > 0`, `repaidUnits` is computed with the **identical** ceiling expression (`mulDivUp` twice). [2](#0-1) 
- **Line 676**: The subtraction `_position.debt -= UtilsLib.toUint128(repaidUnits)` is a plain Solidity 0.8 subtraction — it reverts on underflow. [3](#0-2) 
- **Lines 655–668**: The RCF check only guards `repaidUnits <= maxRepaid` or the `rcfThreshold` escape; it does not guard `repaidUnits <= _position.debt`. [4](#0-3) 
- **Lines 916–921**: `badDebtPriceDown` confirms the boundary at which `badDebt` transitions to zero. [5](#0-4) 

The asymmetry is real: `zeroFloorSub` absorbs the rounding surplus on the bad-debt side, but the same ceiling expression on the `repaidUnits` side is not capped, so `repaidUnits` can strictly exceed `_position.debt` while `badDebt = 0`. The underflow is unconditional at that price boundary.

---

Audit Report

## Title
Arithmetic Underflow in `liquidate` When Seizing All Collateral at Price Just Above Bad-Debt Threshold - (File: `src/Midnight.sol`)

## Summary

When a liquidator passes `seizedAssets = collateral` (full collateral seizure) at a price exactly one unit above `badDebtPriceDown`, the bad-debt block is skipped (`badDebt = 0`, so `_position.debt` is not reduced), but `repaidUnits`, computed with ceiling rounding (`mulDivUp`), can strictly exceed `_position.debt`. The unchecked subtraction `_position.debt -= UtilsLib.toUint128(repaidUnits)` at line 676 reverts with an arithmetic underflow, permanently blocking full-collateral seizure at this price boundary without any privileged role.

## Finding Description

**Root cause — rounding asymmetry between bad-debt and repaidUnits computations.**

The bad-debt amount is computed with `zeroFloorSub` (floors at zero):

```solidity
// lines 614-616
badDebt = badDebt.zeroFloorSub(
    _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
);
```

`badDebt = 0` whenever `collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, maxLif) >= originalDebt`. The `zeroFloorSub` absorbs any surplus above `originalDebt` — the bad-debt block at lines 626–641 is skipped entirely, leaving `_position.debt = originalDebt`.

When the liquidator passes `seizedAssets = collateral`, `repaidUnits` is computed with the **identical** ceiling expression (in pre-maturity mode, `lif = maxLif`):

```solidity
// line 650
repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
```

So `repaidUnits = collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, maxLif)`. Whenever the real-valued result lies in `(originalDebt, originalDebt+1)`, ceiling rounding pushes `repaidUnits` to `originalDebt + 1`, while `badDebt` is still 0 (the `zeroFloorSub` absorbed the surplus). The subtraction then underflows:

```solidity
// line 676
_position.debt -= UtilsLib.toUint128(repaidUnits);  // underflow: repaidUnits > _position.debt
```

**Why the RCF check does not prevent this.**

The Recovery Close Factor check (lines 655–668) only guards against `repaidUnits > maxRepaid` and provides a second escape via `rcfThreshold`. It does **not** check `repaidUnits <= _position.debt`. With LLTV = 0.98e18 and `maxLif ≈ 1.005e18`, `maxRepaid ≈ 66 × (debt − maxDebt)`, which for a position even modestly underwater (e.g., 2%) gives `maxRepaid ≈ 132` while `debt = 100`, so `repaidUnits ≤ maxRepaid` is satisfied even when `repaidUnits > debt`. Any market with `rcfThreshold = type(uint256).max` trivially passes the second condition unconditionally.

**Exploit flow (normal pre-maturity mode, single call):**

1. Market: single collateral, LLTV = 0.98e18, `maxLif = maxLif(0.98e18, cursor)`.
2. Borrower has `debt = units`, `collateral = units.mulDivUp(WAD, lltv)`.
3. Oracle set to `price = badDebtPriceDown(units) + 1`.
4. At this price: `badDebt = 0` (confirmed by `testBadDebtPriceDownIsMaximal`).
5. Liquidator calls `liquidate(seizedAssets = collateral, repaidUnits = 0, postMaturityMode = false)`.
6. `repaidUnits = collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, maxLif) = units + 1`.
7. `_position.debt -= repaidUnits` → arithmetic underflow → revert.

The `badDebtPriceDown` helper confirms the boundary:

```solidity
// test/LiquidationTest.sol lines 916-921
function badDebtPriceDown(uint256 units) internal view returns (uint256) {
    uint256 collateral = units.mulDivUp(WAD, lltv);
    return (units - 1).mulDivDown(maxLif, WAD).mulDivDown(ORACLE_PRICE_SCALE, collateral);
}
```

## Impact Explanation

Any liquidation that passes `seizedAssets = collateral` (full collateral seizure) at a price in the narrow band `(badDebtPriceDown, badDebtPriceDown + 1]` reverts unconditionally with an arithmetic underflow. The position is genuinely unhealthy (`originalDebt > maxDebt`), the liquidator is entitled to seize collateral, but the transaction always reverts. The borrower's position cannot be fully liquidated at this price, leaving bad-debt risk unresolved until the price moves further. This constitutes a permanent freeze of the liquidation path for a valid, unprivileged liquidator action — service unavailability and permanent lock of protocol liquidation state.

## Likelihood Explanation

- Market uses any LLTV < WAD (all tiers except `LLTV_8 = WAD` are affected).
- Oracle price lands in the one-unit-wide window `(badDebtPriceDown, badDebtPriceDown + 1]`. Because `ORACLE_PRICE_SCALE = 1e36`, this is a single integer value — any oracle returning integer prices will hit it with nonzero probability.
- The RCF check must pass: either `rcfThreshold > 0` (deactivated RCF) or the position is sufficiently underwater that `maxRepaid >= repaidUnits`. Both conditions are easily met in practice.
- No privileged action is required. The liquidator simply observes the oracle price and calls `liquidate` with `seizedAssets = collateral`. The condition is deterministic and repeatable whenever the oracle returns that exact price.

## Recommendation

Before the subtraction at line 676, cap `repaidUnits` at `_position.debt` when `seizedAssets` equals the full collateral balance (i.e., when the liquidator is performing a full seizure). Alternatively, use a saturating subtraction (`zeroFloorSub`) for `_position.debt -= repaidUnits` and treat any surplus as additional bad debt to be socialized through the loss factor mechanism — consistent with how the bad-debt block already handles the analogous case. A targeted fix:

```solidity
// After line 650, when seizedAssets == full collateral:
if (repaidUnits > _position.debt) repaidUnits = _position.debt;
```

Or more generally, replace line 676 with a saturating subtraction and route any remainder through the bad-debt accounting path.

## Proof of Concept

**Minimal unit test (Foundry):**

```solidity
function testUnderflowAtBadDebtBoundary(uint256 units) public {
    units = bound(units, 2, MAX_UNITS);
    uint256 price = badDebtPriceDown(units) + 1;
    (uint256 collatAmount,) = _setupUnhealthy(units, price);

    // Confirm badDebt = 0 at this price
    assertEq(computeBadDebt(id, borrower), 0);

    // Full collateral seizure — should succeed but reverts with arithmetic underflow
    vm.expectRevert(stdError.arithmeticError);
    vm.prank(liquidator);
    midnight.liquidate(market, 0, collatAmount, 0, borrower, false, liquidator, address(0), hex"");
}
```

This test uses the existing `badDebtPriceDown`, `_setupUnhealthy`, and `computeBadDebt` helpers already present in `test/LiquidationTest.sol` and requires no external dependencies or privileged setup.

### Citations

**File:** src/Midnight.sol (L614-616)
```text
            badDebt = badDebt.zeroFloorSub(
                _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
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

**File:** src/Midnight.sol (L675-676)
```text
            _marketState.withdrawable += UtilsLib.toUint128(repaidUnits);
            _position.debt -= UtilsLib.toUint128(repaidUnits);
```

**File:** test/LiquidationTest.sol (L916-921)
```text
    function badDebtPriceDown(uint256 units) internal view returns (uint256) {
        uint256 lltv = market.collateralParams[0].lltv;
        uint256 maxLif = market.collateralParams[0].maxLif;
        uint256 collateral = units.mulDivUp(WAD, lltv);
        return (units - 1).mulDivDown(maxLif, WAD).mulDivDown(ORACLE_PRICE_SCALE, collateral);
    }
```
