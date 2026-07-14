### Title
Liquidator can be DoS'd via dust front-run reducing `maxRepaid`, causing `RecoveryCloseFactorConditionsViolated` revert - (File: src/Midnight.sol)

---

### Summary

In `Midnight.sol::liquidate`, the Recovery Close Factor (RCF) cap `maxRepaid` is computed from the borrower's live `_position.debt` at execution time. A competing liquidator (or the borrower themselves) can front-run a legitimate liquidation with a dust-sized liquidation, reducing `_position.debt` by a tiny amount. This shrinks `maxRepaid` below the original liquidator's `repaidUnits`, causing the original transaction to revert with `RecoveryCloseFactorConditionsViolated()`.

---

### Finding Description

In the `liquidate` function, when operating in normal (pre-maturity) mode with `lltv < WAD`, the RCF cap is computed as:

```solidity
uint256 maxRepaid = lltv < WAD
    ? (_position.debt - maxDebt).mulDivUp(WAD * WAD, WAD * WAD - lif * lltv)
    : type(uint256).max;
require(
    repaidUnits <= maxRepaid
        || _position.collateral[collateralIndex]...zeroFloorSub(maxRepaid) < market.rcfThreshold,
    RecoveryCloseFactorConditionsViolated()
);
``` [1](#0-0) 

`maxRepaid` is a function of `_position.debt` at the moment of execution. A front-runner calls `liquidate` with `repaidUnits = 1` (dust) on the same borrower in the same block. This:

1. Reduces `_position.debt` by 1 (line 676).
2. Reduces `_position.collateral[collateralIndex]` by `seizedAssets = 1 * lif / WAD * ORACLE_PRICE_SCALE / price`, which may round to 0 or 1.
3. Recomputes `maxRepaid` at the original liquidator's execution time as `(debt - 1 - maxDebt') * WAD^2 / (WAD^2 - lif*lltv)`, which is strictly less than the original `maxRepaid`. [2](#0-1) 

Because `lltv < WAD`, the denominator `WAD^2 - lif*lltv` is less than `WAD^2`, so each unit of debt reduction shrinks `maxRepaid` by a factor greater than 1 (approximately `WAD^2 / (WAD^2 - lif*lltv) > 1`). For example, with `lltv = 0.8e18` and `lif = 1.1e18`, each 1-unit dust liquidation reduces `maxRepaid` by ~1.67 units.

The second escape condition (`collateral_value/lif - maxRepaid < rcfThreshold`) does not rescue the original liquidator for positions large enough that the RCF is active — after the front-run, `collateral_value/lif - new_maxRepaid` is slightly *larger* than before, so the condition remains unmet.

The original liquidator's transaction then reverts with `RecoveryCloseFactorConditionsViolated()`. [3](#0-2) 

---

### Impact Explanation

- A legitimate liquidator who computed `repaidUnits` equal to the current `maxRepaid` (the maximum allowed by the RCF) is permanently blocked from executing that liquidation in the same block.
- The borrower's unhealthy position persists, accumulating bad debt risk for lenders.
- The attack can be repeated every block at minimal cost (the front-runner pays 1 unit of loan token and receives 0 or 1 unit of collateral due to rounding, a near-zero cost).
- In markets without a `liquidatorGate`, any address can execute the front-run. [4](#0-3) 

---

### Likelihood Explanation

- **No privilege required**: `liquidate` is permissionless when `market.liquidatorGate == address(0)`, which is the default (`address(0) = unrestricted` per the GATES natspec).
- **Realistic attacker**: A competing liquidator who wants to capture the liquidation reward themselves, or a borrower who wants to delay full liquidation (though the borrower would more cheaply use `repay`).
- **Cheap to execute**: The front-runner pays 1 unit of loan token and receives 0–1 unit of collateral (rounding). Gas cost is the only real expense.
- **Requires same-block ordering**: The two transactions must land in the same block, which is achievable via MEV/priority gas auctions on any EVM chain. [5](#0-4) 

---

### Recommendation

Replace the hard revert on `repaidUnits > maxRepaid` with a cap, analogous to the M-13 mitigation of using `min(deficit, amount)`:

```solidity
// Instead of reverting, silently cap repaidUnits to maxRepaid
if (repaidUnits > maxRepaid) {
    repaidUnits = maxRepaid;
    seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
}
```

This ensures a liquidator who requests "up to `maxRepaid`" always succeeds regardless of concurrent partial liquidations in the same block, eliminating the front-run vector. [1](#0-0) 

---

### Proof of Concept

```solidity
function testLiquidateFrontRunDoS() public {
    // Setup: borrower takes debt, position becomes unhealthy
    // ...
    uint256 debtBefore = midnight.debtOf(id, borrower);
    uint256 maxDebtBefore = computeMaxDebt(market, id, borrower);
    // lltv < WAD, so maxRepaid is finite
    uint256 maxRepaid = (debtBefore - maxDebtBefore) * WAD * WAD / (WAD * WAD - lif * lltv);

    // Attacker (competing liquidator) front-runs with dust repaidUnits = 1
    vm.prank(attacker);
    midnight.liquidate(market, collateralIndex, 0, 1, borrower, false, attacker, address(0), "");

    // Original liquidator's tx now reverts: maxRepaid has shrunk by ~1.67 units
    vm.prank(liquidator);
    vm.expectRevert(IMidnight.RecoveryCloseFactorConditionsViolated.selector);
    midnight.liquidate(market, collateralIndex, 0, maxRepaid, borrower, false, liquidator, address(0), "");

    // Borrower's debt only decreased by dust; position remains unhealthy
    assertEq(midnight.debtOf(id, borrower), debtBefore - 1);
}
``` [6](#0-5)

### Citations

**File:** src/Midnight.sol (L581-591)
```text
    function liquidate(
        Market calldata market,
        uint256 collateralIndex,
        uint256 seizedAssets,
        uint256 repaidUnits,
        address borrower,
        bool postMaturityMode,
        address receiver,
        address callback,
        bytes calldata data
    ) external returns (uint256, uint256) {
```

**File:** src/Midnight.sol (L597-600)
```text
        require(
            market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
            LiquidatorGatedFromLiquidating()
        );
```

**File:** src/Midnight.sol (L655-677)
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

            uint128 newCollateral = _position.collateral[collateralIndex] - UtilsLib.toUint128(seizedAssets);
            _position.collateral[collateralIndex] = newCollateral;
            if (newCollateral == 0 && seizedAssets > 0) {
                _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
            }
            _marketState.withdrawable += UtilsLib.toUint128(repaidUnits);
            _position.debt -= UtilsLib.toUint128(repaidUnits);
        }
```
