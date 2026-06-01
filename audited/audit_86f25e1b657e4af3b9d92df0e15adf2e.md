### Title
Post-Maturity `repaidUnits` Input Computes `seizedAssets > collateral`, Causing Underflow Revert and Blocking Bad Debt Realization in Single Call - (File: src/Midnight.sol)

### Summary
In `liquidate()`, when `postMaturityMode = true` and `repaidUnits > 0`, the computed `seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice)` is never checked against `_position.collateral[collateralIndex]` before the subtraction at line 670. The RCF guard that would otherwise bound `repaidUnits` is explicitly skipped in post-maturity mode (`if (!postMaturityMode)`). When `seizedAssets > collateral`, the Solidity 0.8 checked arithmetic underflow reverts the entire transaction, including any bad debt realization that occurred earlier in the same call.

### Finding Description
**Code path** (`src/Midnight.sol`):

1. Lines 620–624: `postMaturityMode` check passes (`block.timestamp > market.maturity`). [1](#0-0) 
2. Lines 626–641: Bad debt is computed and `_position.debt` is reduced by `badDebt`. [2](#0-1) 
3. Line 652: `seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice)` — no upper bound enforced. [3](#0-2) 
4. Lines 655–668: The RCF block is **entirely skipped** because `postMaturityMode = true`. [4](#0-3) 
5. Line 670: `uint128 newCollateral = _position.collateral[collateralIndex] - UtilsLib.toUint128(seizedAssets)` — **underflows and reverts** if `seizedAssets > collateral`. [5](#0-4) 

**Root cause**: No `require(seizedAssets <= _position.collateral[collateralIndex])` guard exists in the post-maturity branch. In normal mode the RCF check implicitly bounds `repaidUnits` (and thus `seizedAssets`) to what the collateral can back. That guard is absent post-maturity.

**Attacker-controlled inputs**: `repaidUnits` (any unprivileged liquidator).

**Exploit flow** (concrete numbers):
- `collateral = 3`, `price = ORACLE_PRICE_SCALE`, `maxLif = 2·WAD`, `originalDebt = 3`, `block.timestamp > market.maturity`, `lif = maxLif`.
- Bad debt loop: `collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, maxLif) = ceil(3/2) = 2`, so `badDebt = 1`, `_position.debt → 2`.
- Liquidator calls `liquidate(..., seizedAssets=0, repaidUnits=2, ..., postMaturityMode=true)`.
- `seizedAssets = 2.mulDivDown(2·WAD, WAD).mulDivDown(ORACLE_PRICE_SCALE, ORACLE_PRICE_SCALE) = 4`.
- Line 670: `3 - 4` → arithmetic underflow → **entire transaction reverts**, including the bad debt realization of 1 unit.

**Why existing checks fail**: `repaidUnits = 2 ≤ _position.debt = 2` so the debt subtraction at line 676 would not underflow; the only guard that could catch this is the RCF block, which is unconditionally skipped post-maturity.

The existing test `testCannotRepayMoreThanDebt` deliberately constrains `liquidationOraclePrice` to be high enough that `seizedAssets` for `repaidUnits = units + 1` stays within collateral, specifically to avoid this scenario. [6](#0-5)  Similarly, `testLiquidateCallback` manually caps `repaid` at `maxRepaid = collateral * price / ORACLE_PRICE_SCALE * WAD / maxLif` before calling `liquidate`. [7](#0-6)  Neither test covers the case where `repaidUnits ≤ debt` but `seizedAssets > collateral`.

### Impact Explanation
Any post-maturity liquidation call that passes `repaidUnits` such that `repaidUnits * lif / WAD * ORACLE_PRICE_SCALE / price > collateral` reverts with an opaque arithmetic underflow. Because the bad debt realization (lines 626–641) executes before the underflow but is rolled back on revert, bad debt is not realized in that transaction. A liquidator who attempts to repay the full remaining debt in a single call (the natural usage) will be blocked whenever `lif` is large relative to the collateral value. The position remains in a bad-debt state, lenders are not slashed, and the `lossFactor` / `totalUnits` accounting is not updated until a separate zero-amount call is made.

### Likelihood Explanation
The condition `debt > collateral * price / ORACLE_PRICE_SCALE * WAD / lif` is exactly the bad-debt condition (using `mulDivUp` rounding). It is reachable whenever:
- A position has bad debt (collateral value at `maxLif` < debt), which is a normal protocol state.
- `lif` is at or near `maxLif` (i.e., `block.timestamp >= market.maturity + TIME_TO_MAX_LIF`, i.e., 15 minutes post-maturity).
- The liquidator passes `repaidUnits` equal to or close to the full remaining debt.

This is the most natural liquidation call pattern (repay all debt, receive proportional collateral). It is repeatable for every such position and requires no special privileges.

### Recommendation
Before line 670, add a cap on `seizedAssets` in post-maturity mode:

```solidity
if (postMaturityMode) {
    uint256 availableCollateral = _position.collateral[collateralIndex];
    if (seizedAssets > availableCollateral) {
        seizedAssets = availableCollateral;
        // recompute repaidUnits from capped seizedAssets (rounding up, favoring protocol)
        repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
    }
}
```

This mirrors the intent of the `seizedAssets`-input path (which naturally cannot exceed collateral since the caller supplies it directly) and ensures the `repaidUnits`-input path is equally safe.

### Proof of Concept
```solidity
function testPostMaturityRepaidUnitsUnderflow() public {
    // Setup: collateral=3, price=ORACLE_PRICE_SCALE, maxLif=2e18, debt=3
    // Use a market with maxLif = 2*WAD
    uint256 units = 3;
    collateralize(market, borrower, units); // supplies collateral proportional to units
    setupMarket(market, units);             // borrower takes on `units` debt

    Oracle(market.collateralParams[0].oracle).setPrice(ORACLE_PRICE_SCALE);
    vm.warp(market.maturity + TIME_TO_MAX_LIF); // lif == maxLif

    // Precondition: position has bad debt (debt > collateral value / maxLif)
    // repaidUnits = remaining debt after bad debt realization = 2
    // seizedAssets computed = 2 * maxLif / WAD * ORACLE_PRICE_SCALE / price = 4 > collateral=3

    // Assert: passing repaidUnits=2 reverts with arithmetic underflow
    vm.expectRevert(stdError.arithmeticError);
    midnight.liquidate(market, 0, 0, 2, borrower, true, address(this), address(0), "");

    // Assert: bad debt was NOT realized (position unchanged)
    assertEq(midnight.debtOf(id, borrower), units); // still 3, not 2

    // Assert: zero-amount call CAN realize bad debt (workaround exists but is non-obvious)
    midnight.liquidate(market, 0, 0, 0, borrower, true, address(this), address(0), "");
    assertEq(midnight.debtOf(id, borrower), 2); // bad debt of 1 now realized
}
```

Expected assertions: first `liquidate` reverts; `debtOf` remains at original value; second zero-amount call succeeds and reduces debt by `badDebt`.

### Citations

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/Midnight.sol (L626-641)
```text
        if (badDebt > 0) {
            // forge-lint: disable-next-item(unsafe-typecast) as badDebt <= _position.debt
            _position.debt -= uint128(badDebt);
            uint256 _totalUnits = _marketState.totalUnits;
            uint256 _lossFactor = _marketState.lossFactor;
            _marketState.lossFactor = UtilsLib.toUint128(
                type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
            );
            _marketState.totalUnits -= UtilsLib.toUint128(badDebt);
            _marketState.continuousFeeCredit = _lossFactor < type(uint128).max
                ? UtilsLib.toUint128(
                    _marketState.continuousFeeCredit
                        .mulDivDown(type(uint128).max - _marketState.lossFactor, type(uint128).max - _lossFactor)
                )
                : 0;
        }
```

**File:** src/Midnight.sol (L651-653)
```text
            } else {
                seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
            }
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

**File:** src/Midnight.sol (L670-671)
```text
            uint128 newCollateral = _position.collateral[collateralIndex] - UtilsLib.toUint128(seizedAssets);
            _position.collateral[collateralIndex] = newCollateral;
```

**File:** test/LiquidationTest.sol (L270-273)
```text
        uint256 maxRepaid = midnight.collateral(id, borrower, collateralIndex)
            .mulDivDown(liquidationOraclePrice, ORACLE_PRICE_SCALE)
            .mulDivDown(WAD, market.collateralParams[collateralIndex].maxLif);
        repaid = bound(repaid, 0, UtilsLib.min(units - expectedBadDebt, maxRepaid));
```

**File:** test/LiquidationTest.sol (L301-308)
```text
        // Price must be high enough that seized assets for (units + 1) don't exceed available collateral.
        uint256 minPrice = (units + 1).mulDivUp(_maxLif, WAD).mulDivUp(ORACLE_PRICE_SCALE, collateral);
        liquidationOraclePrice = bound(liquidationOraclePrice, minPrice, ORACLE_PRICE_SCALE);
        Oracle(market.collateralParams[0].oracle).setPrice(liquidationOraclePrice);

        // Bound repaid above debt but within collateral capacity so the "repay too much" check is reached.
        uint256 maxRepaid = collateral.mulDivDown(liquidationOraclePrice, ORACLE_PRICE_SCALE).mulDivDown(WAD, _maxLif);
        repaid = bound(repaid, units + 1, max(maxRepaid, units + 1));
```
