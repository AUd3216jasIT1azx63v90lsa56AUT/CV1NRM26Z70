Looking at the exact code path, the authorization model, and the protocol's own documentation and formal specs.

### Title
Borrower Self-Activates Reverting-Oracle Collateral to Permanently Block Own Liquidation - (File: src/Midnight.sol)

### Summary
`supplyCollateral` never calls the oracle, so a borrower can activate a collateral slot whose oracle already reverts. `liquidate`'s bitmap loop calls every activated oracle unconditionally with no try/catch; if any one reverts the entire transaction reverts. A borrower who deliberately fills all 16 slots with one reverting-oracle collateral makes their position permanently unliquidatable, violating the invariant that unhealthy positions must remain liquidatable.

### Finding Description
**Exact code path:**

`supplyCollateral` (lines 524–546) gates on `onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender]` and then sets the bitmap bit when `oldCollateral == 0 && assets > 0` — it never queries the oracle. [1](#0-0) 

`liquidate` (lines 607–618) iterates every set bit and calls `IOracle(_collateralParam.oracle).price()` with no error handling: [2](#0-1) 

If that call reverts, the EVM unwinds the entire `liquidate` transaction. The protocol's own NatDev confirms both halves of the primitive: [3](#0-2) 

**Attacker inputs and exploit flow:**

1. Borrower creates or uses a market that has at least one collateral whose oracle is reverting (or will revert — e.g., a deprecated/broken oracle).
2. Borrower calls `supplyCollateral` for 15 normal collateral indices and 1 reverting-oracle index (supplying ≥ 1 wei of each token). The bitmap now has 16 bits set; the `TooManyActivatedCollaterals` guard is satisfied exactly.
3. Borrower borrows against the healthy collaterals via `take`.
4. The position becomes unhealthy (oracle price drops, or borrower borrows to the limit and waits).
5. Any liquidator calls `liquidate(...)`. The while loop reaches the reverting-oracle index, `IOracle(...).price()` reverts, and the entire call reverts.
6. The borrower cannot be liquidated by any liquidator for any `collateralIndex`, because the loop always visits all 16 activated collaterals before the `NotLiquidatable` check.

**Why existing checks fail:**

The authorization guard in `supplyCollateral` prevents a *third party* from poisoning a victim's bitmap, but it does not prevent the borrower from poisoning their *own* bitmap. The `MAX_COLLATERALS_PER_BORROWER` cap (16) is enforced, but the cap is reached exactly — it does not block the reverting slot. There is no try/catch around the oracle call in the liquidation loop, and no mechanism to skip or deactivate a single collateral slot without the borrower's cooperation (which requires calling `withdrawCollateral`, which itself calls `isHealthy`, which also iterates all oracles and reverts on the same broken oracle when debt > 0). [4](#0-3) 

The Certora formal spec explicitly proves this revert propagation as a verified property, confirming it is not guarded: [5](#0-4) 

### Impact Explanation
The unhealthy borrower's position becomes permanently unliquidatable. Lenders in the market cannot recover their principal; bad debt accumulates and is socialized. The borrower retains all non-reverting collateral and is never forced to repay. This directly violates the core invariant: *unhealthy positions remain liquidatable*.

### Likelihood Explanation
The preconditions are low-cost and fully attacker-controlled:
- The borrower only needs to supply 1 wei of the reverting-oracle collateral token (a negligible cost).
- The oracle need not be malicious — any oracle that is deprecated, paused, or temporarily broken suffices; the borrower activates it while it is reverting, which `supplyCollateral` permits.
- The attack is one-time and permanent: once the bitmap is full with the reverting slot, no external party can clear it.
- It is repeatable across any market that contains at least one such collateral parameter.

### Recommendation
Wrap the oracle call inside the liquidation loop in a try/catch and treat a reverting oracle as price = 0 (contributing 0 to `maxDebt` and 0 to `badDebt` reduction), or skip the slot entirely. Alternatively, allow a liquidator to forcibly deactivate a zero-balance or reverting-oracle collateral slot without a health check, so the bitmap can be cleaned up permissionlessly. A third option is to validate at `supplyCollateral` time that the oracle is callable (i.e., call `price()` and require it not to revert), preventing activation of broken-oracle slots in the first place.

### Proof of Concept
```solidity
// Foundry unit test outline
function testRevertingOracleDoSLiquidate() public {
    // 1. Build a market with 16 collateral params; param[15] uses RevertingOracle.
    Market memory m = _createMultiCollateralMarket(16);
    RevertingOracle ro = new RevertingOracle();
    m.collateralParams[15].oracle = address(ro);
    midnight.touchMarket(m);

    // 2. Borrower activates all 16 slots (1 wei each for slot 15).
    vm.startPrank(borrower);
    for (uint256 i = 0; i < 15; i++) {
        deal(m.collateralParams[i].token, borrower, LARGE);
        ERC20(m.collateralParams[i].token).approve(address(midnight), LARGE);
        midnight.supplyCollateral(m, i, LARGE, borrower);
    }
    deal(m.collateralParams[15].token, borrower, 1);
    ERC20(m.collateralParams[15].token).approve(address(midnight), 1);
    midnight.supplyCollateral(m, 15, 1, borrower); // oracle not called here
    vm.stopPrank();

    // 3. Borrow and make position unhealthy.
    setupMarket(m, UNITS);
    Oracle(m.collateralParams[0].oracle).setPrice(0); // drop price

    // 4. Stop the reverting oracle (simulates it being broken post-activation).
    ro.stopOracle();

    // 5. Assert liquidate always reverts.
    vm.expectRevert();
    midnight.liquidate(m, 0, 0, 0, borrower, false, address(this), address(0), "");

    // Also reverts for every other collateralIndex.
    for (uint256 i = 0; i < 15; i++) {
        vm.expectRevert();
        midnight.liquidate(m, i, 0, 0, borrower, false, address(this), address(0), "");
    }

    // Assert borrower still has debt (position not liquidated).
    assertGt(midnight.debtOf(toId(m), borrower), 0);
}
```
Expected assertions: all `liquidate` calls revert; `debtOf` remains positive; the unhealthy position is permanently frozen.

### Citations

**File:** src/Midnight.sol (L34-36)
```text
/// @dev Liquidation reverts if any of the activated collaterals' oracle reverts (see LIVENESS).
/// @dev Note that a borrower can activate a collateral once its oracle is reverting because the oracle is not called in
/// supplyCollateral.
```

**File:** src/Midnight.sol (L523-541)
```text
    /// @dev This function checks authorization to prevent activated collateral poisoning.
    function supplyCollateral(Market memory market, uint256 collateralIndex, uint256 assets, address onBehalf)
        external
    {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        address collateralToken = market.collateralParams[collateralIndex].token;

        Position storage _position = position[id][onBehalf];
        uint256 oldCollateral = _position.collateral[collateralIndex];
        _position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets);

        if (oldCollateral == 0 && assets > 0) {
            uint128 newCollateralBitmap = _position.collateralBitmap.setBit(collateralIndex);
            _position.collateralBitmap = newCollateralBitmap;
            require(
                UtilsLib.countBits(newCollateralBitmap) <= MAX_COLLATERALS_PER_BORROWER, TooManyActivatedCollaterals()
            );
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

**File:** src/Midnight.sol (L948-957)
```text
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
```

**File:** certora/specs/Reverts.spec (L182-193)
```text
/// If any activated collateral oracle reverts on price, liquidate reverts.
rule oracleRevertCausesLiquidateRevert(env e, Midnight.Market market, uint256 collateralIndex, uint256 seizedAssets, uint256 repaidUnits, address borrower, address receiver, address callback, bytes data, uint256 revertingCollateralIndex, bool postMaturityMode) {
    require singleRevertingOracle == market.collateralParams[revertingCollateralIndex].oracle, "oracle is reverting";

    bytes32 id = summaryToId(market);
    uint128 bitmap = collateralBitmap(id, borrower);
    require summaryGetBit(bitmap, revertingCollateralIndex), "revertingCollateralIndex is activated";

    liquidate@withrevert(e, market, collateralIndex, seizedAssets, repaidUnits, borrower, postMaturityMode, receiver, callback, data);

    assert lastReverted;
}
```
