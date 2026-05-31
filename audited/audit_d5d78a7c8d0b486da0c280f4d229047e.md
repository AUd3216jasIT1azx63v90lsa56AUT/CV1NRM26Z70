### Title
Arithmetic Underflow in `liquidate` When Seizing All Collateral at Price Just Above Bad-Debt Threshold - (`src/Midnight.sol`)

### Summary

When a liquidator seizes all collateral at a price exactly one unit above `badDebtPriceDown`, the bad-debt block is skipped (`badDebt = 0`) but `repaidUnits`, computed with ceiling rounding (`mulDivUp`), can strictly exceed `_position.debt`. The subsequent unchecked subtraction `_position.debt -= UtilsLib.toUint128(repaidUnits)` at line 676 reverts with an arithmetic underflow, permanently blocking full-collateral seizure at this price boundary. No privileged role is required; any liquidator can trigger this with a single call.

### Finding Description

**Root cause — rounding asymmetry between bad-debt and repaidUnits computations.**

In `liquidate` (`src/Midnight.sol`), the bad-debt amount is computed with `zeroFloorSub` (floors at zero):

```solidity
// lines 614-616
badDebt = badDebt.zeroFloorSub(
    _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
);
```

`badDebt = 0` whenever `collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, maxLif) >= originalDebt`.

When the liquidator passes `seizedAssets = collateral` (all collateral), `repaidUnits` is computed with the **identical ceiling expression**:

```solidity
// line 650
repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
```

So `repaidUnits = collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, maxLif) >= originalDebt`.

When the ceiling rounding pushes this value **strictly above** `originalDebt` (i.e., the exact real-valued result lies in `(originalDebt, originalDebt+1)`), `badDebt` is still 0 (the bad-debt block is skipped, `_position.debt` remains `originalDebt`), but `repaidUnits > _position.debt`. The unchecked subtraction then underflows:

```solidity
// line 676
_position.debt -= UtilsLib.toUint128(repaidUnits);  // underflow: repaidUnits > _position.debt
```

**Why the RCF check does not prevent this.**

The Recovery Close Factor check (lines 655–668) only guards against `repaidUnits > maxRepaid` and provides a second escape via `rcfThreshold`. It does **not** check `repaidUnits <= _position.debt`. With LLTV = 0.98e18 and `maxLif ≈ 1.005e18` (cursor = 0.25), `WAD² − lif·lltv ≈ 0.0151e36`, giving `maxRepaid ≈ 66 × (debt − maxDebt)`. For a position even modestly underwater (e.g., 2%), `maxRepaid ≈ 132` while `debt = 100`, so `repaidUnits ≤ maxRepaid` is satisfied even when `repaidUnits > debt`. Alternatively, any market with `rcfThreshold = type(uint256).max` (as used in `testLiquidateFullyRepayOrFullySeizeWhenRcfDeactivated`) trivially passes the second condition.

**Exploit flow (normal pre-maturity mode, single call):**

1. Market: single collateral, LLTV = 0.98e18 (`LLTV_7`), `maxLif = maxLif(0.98e18, cursor)`.
2. Borrower has `debt = units`, `collateral = units.mulDivUp(WAD, lltv)`.
3. Oracle set to `price = badDebtPriceDown(units) + 1`.
4. At this price: `badDebt = 0` (position unhealthy but no bad debt).
5. Liquidator calls `liquidate(seizedAssets = collateral, repaidUnits = 0, postMaturityMode = false)`.
6. `repaidUnits = collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, maxLif)`.
7. Due to ceiling rounding, `repaidUnits > units = _position.debt`.
8. `_position.debt -= repaidUnits` → arithmetic underflow → revert.

The optional first call `liquidate(0, 0, false)` mentioned in the question is a no-op at this price (badDebt = 0, no seizure) and does not change the outcome. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

### Impact Explanation

Any liquidation that passes `seizedAssets = collateral` (full collateral seizure) at a price in the narrow band `(badDebtPriceDown, badDebtPriceDown + 1]` reverts unconditionally with an arithmetic underflow. This violates the core invariant that unhealthy positions must remain liquidatable: the position is genuinely unhealthy (`originalDebt > maxDebt`), the liquidator is entitled to seize collateral, but the transaction always reverts. The borrower's position cannot be fully liquidated at this price, leaving bad-debt risk unresolved until the price moves further. [5](#0-4) [6](#0-5) 

### Likelihood Explanation

**Preconditions:**
- Market uses any LLTV < WAD (all tiers except `LLTV_8 = WAD` are affected; LLTV = 0.98e18 is the tightest and most likely to be used for high-quality collateral).
- Oracle price lands in the one-unit-wide window `(badDebtPriceDown, badDebtPriceDown + 1]`. Because `ORACLE_PRICE_SCALE = 1e36`, this window is extremely narrow in relative terms but is a single integer value — any oracle that returns integer prices (e.g., Chainlink with 1e36 scaling) will hit it with nonzero probability.
- The RCF check must pass: either `rcfThreshold > 0` (deactivated RCF) or the position is sufficiently underwater that `maxRepaid >= repaidUnits`.

**Feasibility:** The condition is reachable without any privileged action. The liquidator simply observes the oracle price and calls `liquidate` with `seizedAssets = collateral`. The window is narrow but deterministic and repeatable whenever the oracle returns that exact price. Markets with `rcfThreshold = type(uint256).max` (as tested in the codebase) are unconditionally vulnerable whenever the price hits the boundary. [7](#0-6) [8](#0-7) 

### Recommendation

Cap `repaidUnits` at `_position.debt` before the subtraction. After computing `repaidUnits` from `seizedAssets` (line 650), add:

```solidity
if (repaidUnits > _position.debt) repaidUnits = _position.debt;
```

This mirrors the intent of the bad-debt block (which already handles the case where collateral value exceeds debt via `zeroFloorSub`) and ensures the subtraction never underflows. Alternatively, replace line 676 with:

```solidity
_position.debt = repaidUnits >= _position.debt ? 0 : _position.debt - UtilsLib.toUint128(repaidUnits);
```

The `_marketState.withdrawable += repaidUnits` on line 675 should use the capped value as well to avoid crediting more than the actual debt repaid. [9](#0-8) 

### Proof of Concept

```solidity
// Foundry unit test — add to LiquidationTest.sol
function testFullSeizeAtBadDebtBoundaryUnderflows(uint256 units) public {
    // Use LLTV_7 = 0.98e18 market (single collateral).
    delete market.collateralParams;
    market.collateralParams.push(CollateralParams({
        token: address(collateralToken1),
        lltv: 0.98e18,
        maxLif: maxLif(0.98e18, LIQUIDATION_CURSOR_LOW),
        oracle: address(oracle1)
    }));
    market.rcfThreshold = type(uint256).max; // deactivate RCF
    id = toId(market);

    units = bound(units, 10, MAX_UNITS);
    collateralize(market, borrower, units);
    setupMarket(market, units);

    uint256 price = badDebtPriceDown(units) + 1;
    Oracle(market.collateralParams[0].oracle).setPrice(price);

    // Confirm: no bad debt at this price.
    assertEq(_badDebt(), 0, "no bad debt");
    // Confirm: position is unhealthy (liquidatable).
    assertGt(midnight.debtOf(id, borrower),
        midnight.collateral(id, borrower, 0)
            .mulDivDown(price, ORACLE_PRICE_SCALE)
            .mulDivDown(market.collateralParams[0].lltv, WAD),
        "position is unhealthy");

    uint256 fullCollateral = midnight.collateral(id, borrower, 0);

    // Expected: repaidUnits > debt → underflow → revert.
    vm.expectRevert(stdError.arithmeticError);
    midnight.liquidate(market, 0, fullCollateral, 0, borrower, false, address(this), address(0), "");
}
```

**Expected assertion:** The call reverts with `Arithmetic over/underflow` because `repaidUnits = fullCollateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, maxLif) > units = _position.debt`. [10](#0-9) [11](#0-10)

### Citations

**File:** src/Midnight.sol (L614-616)
```text
            badDebt = badDebt.zeroFloorSub(
                _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
            );
```

**File:** src/Midnight.sol (L643-677)
```text
        if (repaidUnits > 0 || seizedAssets > 0) {
            uint256 _maxLif = market.collateralParams[collateralIndex].maxLif;
            uint256 lif = postMaturityMode
                ? UtilsLib.min(_maxLif, WAD + (_maxLif - WAD) * (block.timestamp - market.maturity) / TIME_TO_MAX_LIF)
                : _maxLif;

            if (seizedAssets > 0) {
                repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
            } else {
                seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
            }

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

**File:** test/LiquidationTest.sol (L218-249)
```text
    function testLiquidateCollateralInput(uint256 units, uint256 seized, uint256 liquidationOraclePrice) public {
        units = bound(units, 1, MAX_UNITS);
        liquidationOraclePrice = bound(liquidationOraclePrice, badDebtPriceDown(units) + 1, ORACLE_PRICE_SCALE);
        collateralize(market, borrower, units);
        setupMarket(market, units);
        uint256 initialCollateral = midnight.collateral(id, borrower, 0);
        seized = bound(
            seized,
            0,
            UtilsLib.min(
                units.mulDivDown(market.collateralParams[0].maxLif, WAD)
                    .mulDivDown(ORACLE_PRICE_SCALE, liquidationOraclePrice),
                initialCollateral
            )
        );
        Oracle(market.collateralParams[0].oracle).setPrice(liquidationOraclePrice);
        vm.warp(market.maturity + TIME_TO_MAX_LIF); // Warp to post-maturity for full LIF.

        (uint256 seizedAssets, uint256 repaidUnits) =
            midnight.liquidate(market, 0, seized, 0, borrower, true, address(this), address(0), "");

        assertEq(
            repaidUnits,
            seized.mulDivUp(liquidationOraclePrice, ORACLE_PRICE_SCALE)
                .mulDivUp(WAD, market.collateralParams[0].maxLif),
            "repaid units"
        );
        assertEq(seizedAssets, seized, "seized assets");

        assertEq(midnight.debtOf(id, borrower), units - repaidUnits, "debt");
        assertEq(midnight.collateral(id, borrower, 0), initialCollateral - seizedAssets, "collateral");
    }
```

**File:** test/LiquidationTest.sol (L292-312)
```text
    function testCannotRepayMoreThanDebt(uint256 units, uint256 repaid, uint256 liquidationOraclePrice) public {
        units = bound(units, 10, MAX_UNITS - 1);
        collateralize(market, borrower, units);
        setupMarket(market, units);
        vm.warp(market.maturity + TIME_TO_MAX_LIF); // Warp to post-maturity for full LIF.

        uint256 _maxLif = market.collateralParams[0].maxLif;
        uint256 collateral = midnight.collateral(id, borrower, 0);

        // Price must be high enough that seized assets for (units + 1) don't exceed available collateral.
        uint256 minPrice = (units + 1).mulDivUp(_maxLif, WAD).mulDivUp(ORACLE_PRICE_SCALE, collateral);
        liquidationOraclePrice = bound(liquidationOraclePrice, minPrice, ORACLE_PRICE_SCALE);
        Oracle(market.collateralParams[0].oracle).setPrice(liquidationOraclePrice);

        // Bound repaid above debt but within collateral capacity so the "repay too much" check is reached.
        uint256 maxRepaid = collateral.mulDivDown(liquidationOraclePrice, ORACLE_PRICE_SCALE).mulDivDown(WAD, _maxLif);
        repaid = bound(repaid, units + 1, max(maxRepaid, units + 1));

        vm.expectRevert(stdError.arithmeticError);
        midnight.liquidate(market, 0, 0, repaid, borrower, true, address(this), address(0), "");
    }
```

**File:** test/LiquidationTest.sol (L336-343)
```text
    function testBadDebtPriceDownIsMaximal(uint256 units) public {
        units = bound(units, 10, MAX_UNITS);
        collateralize(market, borrower, units);
        setupMarket(market, units);
        Oracle(market.collateralParams[0].oracle).setPrice(badDebtPriceDown(units) + 1);

        assertEq(_badDebt(), 0, "should have no bad debt at badDebtPriceDown");
    }
```

**File:** test/LiquidationTest.sol (L661-718)
```text
    function testLiquidateFullyRepayOrFullySeizeWhenRcfDeactivated(
        uint256 units,
        uint256 collateral1,
        uint256 collateral2
    ) public {
        collateral1 = bound(collateral1, 1, MAX_UNITS);
        collateral2 = bound(collateral2, 1, MAX_UNITS);

        // Deactivate RCF.
        market.rcfThreshold = type(uint256).max;
        id = toId(market);

        // Price is 1 initially, assume liquidatable but no bad debt.
        uint256 maxDebt = collateral1.mulDivDown(market.collateralParams[0].lltv, WAD)
            + collateral2.mulDivDown(market.collateralParams[1].lltv, WAD);
        uint256 repayableDebt = collateral1.mulDivDown(WAD, market.collateralParams[0].maxLif)
            + collateral2.mulDivDown(WAD, market.collateralParams[1].maxLif);
        units = bound(units, maxDebt, repayableDebt);
        vm.assume(units > maxDebt);

        // Write debt into Position storage.
        // Layout: slot 0 = credit | pendingFee, slot 1 = lastLossFactor | lastAccrual,
        // slot 2 = debt | collateralBitmap.
        // Debt is in the lower 128 bits of slot 2.
        uint256 mappingSlot = 0;
        bytes32 intermediateSlot = keccak256(abi.encode(id, mappingSlot));
        bytes32 borrowerSlot = keccak256(abi.encode(borrower, intermediateSlot));
        vm.store(address(midnight), bytes32(uint256(borrowerSlot) + 2), bytes32(units));

        assertEq(midnight.debtOf(id, borrower), units, "debt");

        // Collateralize with both collateralParams.

        vm.prank(borrower);

        midnight.setIsAuthorized(address(this), true, borrower);

        deal(market.collateralParams[0].token, address(this), collateral1);
        midnight.supplyCollateral(market, 0, collateral1, borrower);

        deal(market.collateralParams[1].token, address(this), collateral2);
        midnight.supplyCollateral(market, 1, collateral2, borrower);

        // Check that the position has no bad debt.
        // If it had bad debt, this can be taken into account separately.
        assertEq(_badDebt(), 0, "no bad debt");

        uint256 collateralNeededToRepayAll = units.mulDivDown(market.collateralParams[0].maxLif, WAD);
        if (collateralNeededToRepayAll <= collateral1) {
            midnight.liquidate(market, 0, 0, units, borrower, false, address(this), address(0), "");
        } else {
            midnight.liquidate(market, 0, collateral1, 0, borrower, false, address(this), address(0), "");
        }

        uint256 debtAfter = midnight.debtOf(id, borrower);
        uint256 collateralAfter = midnight.collateral(id, borrower, 0);
        assertTrue(debtAfter == 0 || collateralAfter == 0, "either debt repaid or collateral seized");
    }
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

**File:** src/libraries/ConstantsLib.sol (L36-37)
```text
uint256 constant LLTV_7 = 0.98e18;
uint256 constant LLTV_8 = 1e18;
```
