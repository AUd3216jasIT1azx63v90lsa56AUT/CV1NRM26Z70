### Title
Borrower Can Permanently Bypass Liquidation by Activating a Collateral with a Reverting Oracle — (`src/Midnight.sol`)

### Summary
`liquidate()` iterates over every collateral in a borrower's `collateralBitmap` and calls `IOracle(...).price()` for each one. If any single oracle reverts, the entire liquidation reverts. Because `supplyCollateral()` never calls the oracle, a borrower can deliberately activate a collateral whose oracle is already reverting by depositing even 1 wei of that token, permanently shielding their position from liquidation.

### Finding Description

**Root cause — the loop in `liquidate` has no oracle-skip path:**

`liquidate()` unconditionally calls `IOracle(_collateralParam.oracle).price()` for every bit set in `_collateralBitmap`: [1](#0-0) 

If any oracle in that loop reverts, the whole transaction reverts. There is no `try/catch`, no skip flag, and no way for the liquidator to exclude a specific collateral from the health-check loop.

**The activation door — `supplyCollateral` never calls the oracle:** [2](#0-1) 

Supplying `assets > 0` for a collateral that has `oldCollateral == 0` sets the corresponding bit in `collateralBitmap`. The oracle is never consulted. The protocol itself documents this asymmetry: [3](#0-2) 

**Exploit path:**

1. Market `M` has collaterals `[A, B]` with oracles `[oracleA, oracleB]`.
2. Borrower opens a debt position using collateral A (oracleA is healthy).
3. `oracleB` begins reverting (deprecated, upgraded, or compromised — an external event).
4. Borrower acquires 1 wei of collateral-B token (trivially available on any DEX).
5. Borrower calls `supplyCollateral(M, indexB, 1, self)`. No oracle is called; bit `indexB` is set in `collateralBitmap`.
6. Any subsequent call to `liquidate(M, ..., borrower, ...)` enters the while-loop, hits `IOracle(oracleB).price()`, and reverts unconditionally.
7. `isHealthy` and `withdrawCollateral` also revert for the same reason, so the borrower cannot be forced out through any path. [4](#0-3) 

### Impact Explanation

A borrower with an unhealthy (or post-maturity overdue) position becomes permanently unliquidatable. Their debt is never repaid, bad debt accumulates in the market, and lenders suffer proportional credit losses via the loss-factor mechanism. This is a direct, irreversible loss of lender funds — the highest-severity impact class listed in `live_context.json` ("bad debt creation", "liquidation bypass").

### Likelihood Explanation

- **No privileged access required.** Any borrower can execute this.
- **Cost is 1 wei** of the reverting collateral token, which is trivially obtainable.
- **Oracle revert is a realistic precondition.** Oracles are external contracts that can be deprecated, paused, or exploited. The protocol's own LIVENESS section acknowledges this scenario explicitly.
- The attack is most attractive when the borrower's position is already deeply underwater and liquidation would wipe out their collateral.

### Recommendation

Add a `try/catch` around each oracle call inside the `liquidate` loop, treating a reverting oracle as returning price `0` (or skipping that collateral's contribution to `maxDebt`). Alternatively, allow liquidators to pass a subset of collateral indices to evaluate, so a single reverting oracle does not block liquidation of the remaining healthy collaterals. The `isHealthy` path used by `withdrawCollateral` and `take` should receive the same treatment.

### Proof of Concept

```
Precondition: market M has collaterals [A, B]; oracleB reverts.

1. borrower.supplyCollateral(M, indexA, 100e18, borrower)   // healthy oracle, activates A
2. borrower.take(...)                                        // borrows against A
3. oracleB starts reverting (external event)
4. borrower.supplyCollateral(M, indexB, 1, borrower)        // no oracle call → activates B
5. liquidator.liquidate(M, indexA, 0, repaidUnits, borrower, false, ...)
   → enters while(_collateralBitmap != 0)
   → hits IOracle(oracleB).price()  ← REVERTS
   → liquidation permanently blocked
6. borrower retains debt indefinitely; lenders absorb bad debt.
``` [5](#0-4) [6](#0-5)

### Citations

**File:** src/Midnight.sol (L143-145)
```text
/// @dev If an activated collateral oracle reverts on price, liquidate reverts.
/// @dev If an activated collateral oracle reverts on price, isHealthy, withdrawCollateral and take revert when the user
/// (seller for take) has non-zero debt.
```

**File:** src/Midnight.sol (L524-546)
```text
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

        emit EventsLib.SupplyCollateral(msg.sender, id, collateralToken, assets, onBehalf);

        SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
    }
```

**File:** src/Midnight.sol (L606-618)
```text
        uint128 _collateralBitmap = _position.collateralBitmap;
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

**File:** src/Midnight.sol (L944-957)
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
```
