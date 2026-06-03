Audit Report

## Title
Borrower Self-Immunizes Against Liquidation by Activating a Reverting-Oracle Collateral Slot - ([File: src/Midnight.sol])

## Summary
`supplyCollateral` performs no oracle call and no health check, allowing an unhealthy borrower to activate a new collateral slot backed by a reverting oracle at any time. Because `liquidate` unconditionally calls `IOracle(_collateralParam.oracle).price()` on every bit in `collateralBitmap` with no `try/catch`, a single reverting oracle permanently blocks all liquidation attempts, leaving bad debt frozen and lenders unable to recover funds.

## Finding Description

**Root cause — `supplyCollateral` lacks a health check, enabling a borrower to self-poison their own collateral bitmap.**

`supplyCollateral` carries the NatSpec comment "This function checks authorization to prevent activated collateral poisoning," but the authorization check only prevents *external* parties from poisoning a borrower's collateral. The borrower calling on their own behalf satisfies `onBehalf == msg.sender` and is never blocked. [1](#0-0) 

When activating a new slot, the only additional guard is the 16-collateral cap; no oracle is consulted and no health check is performed: [2](#0-1) 

`liquidate` iterates every set bit in `collateralBitmap` and calls `IOracle(_collateralParam.oracle).price()` on each with no `try/catch` or skip-on-revert logic: [3](#0-2) 

`isHealthy` has the identical pattern, so `withdrawCollateral` (which calls `isHealthy`) also reverts when the borrower has debt and any activated oracle reverts: [4](#0-3) 

**Exploit flow:**
1. A multi-collateral market exists with `collateralParams[0]` (healthy oracle) and `collateralParams[1]` (oracle that reverts — e.g., Chainlink with a triggered circuit-breaker, or a purpose-deployed contract).
2. Borrower supplies collateral at index 0 and borrows via `take`.
3. Price of index-0 collateral drops; borrower becomes unhealthy.
4. Borrower calls `supplyCollateral(market, 1, 1, borrower)`. No oracle is called; 1 wei of the index-1 token is transferred in and bit 1 is set in `collateralBitmap`. Succeeds unconditionally.
5. Any subsequent `liquidate(market, 0, ...)` enters the bitmap loop, hits `IOracle(collateralParams[1].oracle).price()`, which reverts, and the entire transaction reverts.
6. The position is permanently unliquidatable.

**Why existing checks do not stop it:**
- The `Unauthorized` check is satisfied because the borrower calls on their own behalf.
- `TooManyActivatedCollaterals` only fires at 17 active slots.
- There is no health check in `supplyCollateral`.
- There is no `try/catch` or skip-on-revert logic in the `liquidate` bitmap loop.

The protocol's own NatSpec explicitly documents the liveness consequence and the capability that enables the attack: [5](#0-4) [6](#0-5) 

The Certora formal verification spec formally proves `oracleRevertCausesLiquidateRevert` as a verified property, confirming the team is aware of the revert behavior, but the self-poisoning path is not addressed: [7](#0-6) 

The test suite ships a `RevertingOracle` contract confirming awareness of reverting oracles: [8](#0-7) 

## Impact Explanation
An unhealthy borrower with outstanding debt becomes permanently unliquidatable. Their debt remains on the books, `marketState.totalUnits` is never reduced via bad-debt socialization, and lenders cannot recover their funds. Every lender in the market suffers a loss proportional to the bad debt that cannot be realized. The `withdrawable` pool is never replenished, so lenders who try to withdraw after maturity also find insufficient assets. This constitutes a permanent freeze of lender funds and unrecoverable bad debt — a concrete, in-scope critical impact.

## Likelihood Explanation
**Preconditions:**
- A multi-collateral market where at least one collateral's oracle can revert. This is realistic: Chainlink aggregators revert when the sequencer is down (L2s), when the answer is stale beyond a configured threshold, or when a circuit-breaker fires. Custom oracles may revert on price deviation. The market need not be created by the attacker.
- The borrower must hold at least 1 wei of the reverting-oracle collateral token — trivially achievable for any ERC-20 with non-zero supply.
- The borrower must act before being liquidated, i.e., between becoming unhealthy and the first successful liquidation call. Oracle outages are often sustained (minutes to hours), making this window realistic.

**Repeatability:** The attack is permanent once executed. The borrower cannot be forced to withdraw the 1-wei collateral (only they or an authorized address can call `withdrawCollateral`, and that function also reverts when the borrower has debt and any oracle reverts). [9](#0-8) 

## Recommendation
Add a health check at the end of `supplyCollateral` when the borrower has existing debt, or alternatively add `try/catch` wrapping around each oracle call in the `liquidate` and `isHealthy` bitmap loops so that a reverting oracle is treated as returning price 0 (or skipped) rather than causing a full revert. The former is simpler and consistent with `withdrawCollateral`'s existing pattern. The latter is more robust but changes liquidation semantics for all oracle failure modes.

## Proof of Concept
```solidity
// 1. Deploy a RevertingOracle (already exists in test suite at test/helpers/RevertingOracle.sol).
// 2. Create a two-collateral market: collateralParams[0] = normal oracle, collateralParams[1] = RevertingOracle (not yet stopped).
// 3. Borrower supplies collateral at index 0, borrows via take.
// 4. Drop oracle0 price so borrower is unhealthy.
// 5. Call RevertingOracle.stopOracle() to make it revert.
// 6. Borrower calls supplyCollateral(market, 1, 1, borrower) — succeeds, sets bit 1 in collateralBitmap.
// 7. Call liquidate(market, 0, 0, 0, borrower, false, ...) — reverts because the bitmap loop hits the stopped oracle at index 1.
// 8. Assert: borrower's debt is unchanged, liquidation is permanently blocked.
```

### Citations

**File:** src/Midnight.sol (L34-36)
```text
/// @dev Liquidation reverts if any of the activated collaterals' oracle reverts (see LIVENESS).
/// @dev Note that a borrower can activate a collateral once its oracle is reverting because the oracle is not called in
/// supplyCollateral.
```

**File:** src/Midnight.sol (L143-144)
```text
/// @dev If an activated collateral oracle reverts on price, liquidate reverts.
/// @dev If an activated collateral oracle reverts on price, isHealthy, withdrawCollateral and take revert when the user
```

**File:** src/Midnight.sol (L523-527)
```text
    /// @dev This function checks authorization to prevent activated collateral poisoning.
    function supplyCollateral(Market memory market, uint256 collateralIndex, uint256 assets, address onBehalf)
        external
    {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L535-541)
```text
        if (oldCollateral == 0 && assets > 0) {
            uint128 newCollateralBitmap = _position.collateralBitmap.setBit(collateralIndex);
            _position.collateralBitmap = newCollateralBitmap;
            require(
                UtilsLib.countBits(newCollateralBitmap) <= MAX_COLLATERALS_PER_BORROWER, TooManyActivatedCollaterals()
            );
        }
```

**File:** src/Midnight.sol (L556-568)
```text
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        address collateralToken = market.collateralParams[collateralIndex].token;

        Position storage _position = position[id][onBehalf];
        uint256 newCollateral = _position.collateral[collateralIndex] - assets;
        _position.collateral[collateralIndex] = UtilsLib.toUint128(newCollateral);

        if (newCollateral == 0 && assets > 0) {
            _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
        }

        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());
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

**File:** certora/specs/Reverts.spec (L183-193)
```text
rule oracleRevertCausesLiquidateRevert(env e, Midnight.Market market, uint256 collateralIndex, uint256 seizedAssets, uint256 repaidUnits, address borrower, address receiver, address callback, bytes data, uint256 revertingCollateralIndex, bool postMaturityMode) {
    require singleRevertingOracle == market.collateralParams[revertingCollateralIndex].oracle, "oracle is reverting";

    bytes32 id = summaryToId(market);
    uint128 bitmap = collateralBitmap(id, borrower);
    require summaryGetBit(bitmap, revertingCollateralIndex), "revertingCollateralIndex is activated";

    liquidate@withrevert(e, market, collateralIndex, seizedAssets, repaidUnits, borrower, postMaturityMode, receiver, callback, data);

    assert lastReverted;
}
```

**File:** test/helpers/RevertingOracle.sol (L5-16)
```text
contract RevertingOracle {
    uint256 internal _price = 1e36;
    bool internal stopped;

    function price() external view returns (uint256) {
        require(!stopped, "Oracle should not be called");
        return _price;
    }

    function stopOracle() external {
        stopped = true;
    }
```
