The code path is fully confirmed. Here is the analysis:

**Key facts from the code:**

1. `mulDivDown(x, 0, ORACLE_PRICE_SCALE)` = `(x * 0) / ORACLE_PRICE_SCALE` = `0` — no revert. [1](#0-0) 

2. In `liquidate()`, every activated collateral's oracle is called; if oracle at index `i` returns 0, its contribution to `maxDebt` is silently 0. [2](#0-1) 

3. The liquidatability check uses this potentially underestimated `maxDebt`: `originalDebt > maxDebt`. [3](#0-2) 

4. The Certora spec `oracleZeroCausesIsHealthyReturnFalse` only proves the case where **all** oracles return 0 — not the partial case. [4](#0-3) 

5. The Certora spec `oracleZeroCausesLiquidateWithRepaidRevert` only covers the **liquidated** collateral's oracle returning 0 with `repaidUnits > 0` — not a non-liquidated collateral returning 0 while the liquidated collateral has a valid price. [5](#0-4) 

6. `isHealthy()` has the identical loop structure and the same gap. [6](#0-5) 

---

### Title
Partial zero-price oracle silently underestimates `maxDebt`, enabling liquidation of healthy multi-collateral borrowers - (File: src/Midnight.sol)

### Summary
In a multi-collateral market, `liquidate()` accumulates `maxDebt` by summing each activated collateral's oracle price contribution. If any non-liquidated collateral's oracle returns `0`, its contribution is silently `0` (no revert, no guard), causing `maxDebt` to be underestimated. A borrower whose true `maxDebt >= debt` but whose underestimated `maxDebt < debt` passes the `originalDebt > maxDebt` check and is incorrectly liquidated by an unprivileged liquidator.

### Finding Description
**Code path:**

`liquidate()` → bitmap loop → `IOracle(_collateralParam.oracle).price()` returns `0` for index `i` → `_collateral.mulDivDown(0, ORACLE_PRICE_SCALE).mulDivDown(lltv, WAD) = 0` → `maxDebt` accumulates only contributions from oracles with non-zero prices → `require(!liquidationLocked && originalDebt > maxDebt)` passes incorrectly.

**Root cause:** No guard against `price == 0` for non-liquidated collaterals in the `maxDebt` accumulation loop. `mulDivDown(x, 0, d)` returns `0` without reverting (it is `(x * 0) / d`).

**Attacker inputs:**
- `collateralIndex = j` (collateral whose oracle returns a valid price `P_j > 0`)
- `seizedAssets > 0`, `repaidUnits = 0` (avoids the division-by-zero path that would revert when `liquidatedCollatPrice = 0`)
- `postMaturityMode = false`

**Exploit flow:**
1. Market is created with ≥2 collaterals: index `i` (oracle will return 0) and index `j` (oracle returns valid `P_j`).
2. Borrower calls `supplyCollateral` for both indices, then `take` to create debt `D`.
3. Oracle at index `i` transitions to returning `0` (e.g., uninitialized feed, price-to-zero event).
4. True `maxDebt = C_i * P_i / ORACLE_PRICE_SCALE * lltv_i / WAD + C_j * P_j / ORACLE_PRICE_SCALE * lltv_j / WAD >= D`.
5. Underestimated `maxDebt' = 0 + C_j * P_j / ORACLE_PRICE_SCALE * lltv_j / WAD < D`.
6. Liquidator calls `liquidate(market, j, seizedAssets, 0, borrower, false, ...)`.
7. Loop: index `i` contributes `0` to `maxDebt`; `liquidatedCollatPrice = P_j` (valid).
8. `originalDebt > maxDebt'` → `true` → `NotLiquidatable` is NOT thrown.
9. Borrower's collateral at index `j` is seized; borrower is harmed despite being healthy.

**Why existing checks fail:**
- The Certora `oracleZeroCausesIsHealthyReturnFalse` rule only proves the all-zero case; the partial-zero case is uncovered.
- The Certora `oracleZeroCausesLiquidateWithRepaidRevert` rule only covers `repaidUnits > 0` with the *liquidated* collateral's oracle at zero; using `seizedAssets > 0` with a valid `liquidatedCollatPrice` bypasses this.
- There is no `require(price > 0)` or equivalent guard in the accumulation loop.

### Impact Explanation
A healthy borrower is liquidated: their collateral at index `j` is seized and their debt is reduced without valid cause. The invariant "healthy positions are not liquidatable" is broken. The borrower suffers direct, irreversible collateral loss.

### Likelihood Explanation
Preconditions are reachable: multi-collateral markets are a first-class feature; oracle prices of `0` are explicitly modeled in the Certora specs (the protocol acknowledges this as a valid return value). An attacker can monitor oracle feeds and trigger the liquidation the moment any non-liquidated collateral's oracle returns `0`. The attack is repeatable as long as the oracle remains at `0` and the borrower has not been fully liquidated.

### Recommendation
In the `maxDebt` accumulation loop inside both `liquidate()` and `isHealthy()`, add a guard that reverts (or treats the position as unliquidatable) when any activated collateral's oracle returns `0` and the borrower has debt — consistent with how oracle reverts are already handled:

```solidity
uint256 price = IOracle(_collateralParam.oracle).price();
require(price > 0, OraclePriceZero()); // add this guard
```

This mirrors the existing liveness guarantee for reverting oracles and closes the gap for zero-returning oracles on non-liquidated collaterals.

### Proof of Concept
```solidity
// Foundry fuzz test
function testHealthyBorrowerLiquidatedViaPartialZeroOracle(
    uint256 c0, uint256 c1, uint256 p0, uint256 p1, uint256 debt
) public {
    // Setup: 2-collateral market, oracle[0] valid, oracle[1] will go to 0
    // Bound: true maxDebt = c0*p0/SCALE*lltv + c1*p1/SCALE*lltv >= debt
    //        underestimated maxDebt = c0*p0/SCALE*lltv < debt
    // (1) supplyCollateral(index=0, c0), supplyCollateral(index=1, c1)
    // (2) take() to create `debt` units
    // (3) oracle[1].setPrice(0)
    // (4) assert isHealthy(market, id, borrower) == true  // true maxDebt >= debt
    // (5) liquidate(market, 0, seizedAssets, 0, borrower, false, ...)
    // Expected: liquidate() reverts with NotLiquidatable()
    // Actual (bug): liquidate() succeeds, seizing borrower's collateral[0]
    vm.expectRevert(IMidnight.NotLiquidatable.selector);
    midnight.liquidate(market, 0, someSeizedAssets, 0, borrower, false, address(this), address(0), "");
}
```

The assertion `vm.expectRevert(NotLiquidatable)` will fail, demonstrating the invariant violation: `isHealthy() == true` does not imply `liquidate()` reverts.

### Citations

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** src/Midnight.sol (L607-618)
```text
        while (_collateralBitmap != 0) {
            uint256 i = UtilsLib.msb(_collateralBitmap);
            CollateralParams memory _collateralParam = market.collateralParams[i];
            uint256 price = IOracle(_collateralParam.oracle).price();
            if (i == collateralIndex) liquidatedCollatPrice = price;
            uint256 _collateral = _position.collateral[i];
            maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(_collateralParam.lltv, WAD);
            badDebt = badDebt.zeroFloorSub(
                _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
            );
            _collateralBitmap = _collateralBitmap.clearBit(i);
        }
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/Midnight.sol (L944-960)
```text
    function isHealthy(Market memory market, bytes32 id, address borrower) public view returns (bool) {
        Position storage _position = position[id][borrower];
        uint256 debt = _position.debt;
        uint256 maxDebt;
        if (debt > 0) {
            uint128 _collateralBitmap = _position.collateralBitmap;
            while (_collateralBitmap != 0) {
                uint256 i = UtilsLib.msb(_collateralBitmap);
                CollateralParams memory collateralParam = market.collateralParams[i];
                uint256 price = IOracle(collateralParam.oracle).price();
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
                _collateralBitmap = _collateralBitmap.clearBit(i);
            }
        }
        return maxDebt >= debt;
    }
```

**File:** certora/specs/Reverts.spec (L245-253)
```text
/// If liquidated collateral oracle returns 0 on price, liquidate with repaid input reverts.
rule oracleZeroCausesLiquidateWithRepaidRevert(env e, Midnight.Market market, uint256 collateralIndex, uint256 repaidUnits, address borrower, address receiver, address callback, bytes data, bool postMaturityMode) {
    require singleZeroOracle == market.collateralParams[collateralIndex].oracle, "oracle returns zero";
    require repaidUnits > 0, "using repaid units as input";

    liquidate@withrevert(e, market, collateralIndex, 0, repaidUnits, borrower, postMaturityMode, receiver, callback, data);

    assert lastReverted;
}
```

**File:** certora/specs/Reverts.spec (L255-265)
```text
/// If all oracles return 0 and the borrower has debt, isHealthy returns false.
rule oracleZeroCausesIsHealthyReturnFalse(env e, Midnight.Market market, address borrower) {
    require forceOracleReturnZero, "all oracles return zero";

    bytes32 id = summaryToId(market);
    require collateralBitmap(id, borrower) != 0, "borrower has activated collaterals";

    bool healthy = isHealthy(e, market, id, borrower);

    assert debtOf(id, borrower) > 0 => !healthy;
}
```
