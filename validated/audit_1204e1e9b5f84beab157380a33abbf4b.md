All technical claims in the report check out against the actual code. Here is the validation result:

---

Audit Report

## Title
Borrower can self-immunize against liquidation by activating a reverting-oracle collateral via `supplyCollateral` - (File: `src/Midnight.sol`)

## Summary
`supplyCollateral` sets a collateral bitmap bit with no oracle liveness check, allowing a borrower to activate a reverting-oracle collateral on their own position. Because `liquidate` iterates every bit in `collateralBitmap` and calls `IOracle.price()` unconditionally with no try/catch, a single reverting oracle permanently blocks all liquidation attempts. The borrower retains full access to `repay`, which never calls any oracle, and can repay at a time of their choosing or never repay, leaving unrecoverable bad debt.

## Finding Description

**Root cause — `supplyCollateral` activates a bitmap bit without any oracle liveness check:** [1](#0-0) 

When `oldCollateral == 0 && assets > 0`, the bit is set unconditionally. No oracle call, no health check, and no check that the newly activated oracle is functional is performed.

**Authorization check only stops third-party poisoning:** [2](#0-1) 

The NatSpec comment at line 523 explicitly states the check is to prevent "activated collateral poisoning," but `require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender])` is trivially satisfied by the borrower acting on their own position.

**`liquidate` calls every activated oracle unconditionally:** [3](#0-2) 

The bitmap loop at line 607 calls `IOracle(_collateralParam.oracle).price()` at line 610 with no try/catch. A single reverting oracle causes the entire `liquidate` call to revert before the `NotLiquidatable` check at line 620 is even reached. [4](#0-3) 

**`isHealthy` has the same unconditional oracle call:** [5](#0-4) 

`withdrawCollateral` clears the bit for the withdrawn collateral index before calling `isHealthy`. [6](#0-5) 

However, since the borrower is unhealthy, `isHealthy` returns false and reverts with `UnhealthyBorrower()` regardless — the borrower cannot undo the attack by withdrawing the reverting collateral while their position remains unhealthy.

**`repay` never calls any oracle:** [7](#0-6) 

`repay` only decrements `_position.debt` and increments `marketState[id].withdrawable`. The borrower can repay at any time after self-immunizing.

**The Certora spec formally proves this execution path:** [8](#0-7) 

`oracleRevertCausesLiquidateRevert` formally asserts that if any activated collateral oracle reverts, `liquidate` reverts — treating it as a guaranteed system property, not a guarded edge case. The spec comment at line 34 further acknowledges that re-entrant callbacks cannot deactivate collaterals without hitting the same reverting oracle through `withdrawCollateral → isHealthy`. [9](#0-8) 

**The protocol's own NatSpec explicitly acknowledges this attack vector:** [10](#0-9) 

Lines 34–36 state: *"Note that a borrower can activate a collateral once its oracle is reverting because the oracle is not called in `supplyCollateral`."* The LIVENESS section also explicitly lists this as a known behavior. [11](#0-10) 

**Exploit call sequence:**
1. Borrower calls `take` to open a debt position.
2. Oracle price drops → borrower becomes unhealthy (`debt > maxDebt`).
3. Borrower calls `supplyCollateral(market, idx_reverting, 1, borrower)` where `market.collateralParams[idx_reverting].oracle` always reverts on `.price()`.
4. Bit `idx_reverting` is now set in `_position.collateralBitmap`.
5. Any `liquidate(market, ...)` call for this borrower enters the bitmap loop, hits the reverting oracle at line 610, and reverts.
6. Borrower repays at leisure when market conditions are favorable, or never repays, leaving unrecoverable bad debt.

## Impact Explanation
An unhealthy borrower can permanently block all liquidation attempts by activating a single reverting-oracle collateral with 1 wei. This directly violates the core protocol invariant that unhealthy positions remain liquidatable. The borrower converts a forced liquidation into a voluntary repayment, eliminating the protocol's ability to protect lenders from bad debt accumulation. If the borrower never repays, the bad debt cannot be realized through the normal liquidation path.

## Likelihood Explanation
The attack requires: (1) a market with at least one `collateralParams` entry whose oracle can revert (circuit-breaker oracles, deprecated feeds, or any oracle that reverts under certain conditions — explicitly modeled in the Certora specs as a realistic scenario); (2) the borrower holds 1 wei of the corresponding collateral token (trivially acquirable); (3) the borrower is unhealthy. The borrower has a direct financial incentive to execute this attack to avoid forced liquidation at a loss. The attack requires a single `supplyCollateral` call and is repeatable across any vulnerable market.

## Recommendation
Add an oracle liveness check in `supplyCollateral` when activating a new collateral bit: call `IOracle(market.collateralParams[collateralIndex].oracle).price()` and require it to succeed (non-reverting, non-zero) before setting the bitmap bit. Alternatively, require that the borrower's position remains healthy after activating a new collateral (calling `isHealthy` post-activation), which would also prevent an unhealthy borrower from activating any new collateral at all. A try/catch guard in the `liquidate` bitmap loop is a weaker mitigation that would prevent DoS but could allow liquidators to skip reverting-oracle collaterals, potentially undervaluing the position.

## Proof of Concept
1. Deploy a mock oracle that always reverts on `price()`.
2. Create a market with two collateral params: one with a valid oracle, one with the reverting mock oracle.
3. Have a borrower supply valid collateral and call `take` to open a debt position.
4. Manipulate the valid oracle price downward so the borrower becomes unhealthy.
5. Borrower calls `supplyCollateral(market, revertingOracleIndex, 1, borrower)` — succeeds with no oracle check.
6. Attempt `liquidate(market, validOracleIndex, ...)` — reverts because the bitmap loop hits the reverting oracle at line 610 before any liquidatability check.
7. Confirm `repay` still succeeds for the borrower.
8. The Certora rule `oracleRevertCausesLiquidateRevert` in `certora/specs/Reverts.spec` lines 183–193 is a directly runnable formal proof of step 6.

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

**File:** src/Midnight.sol (L508-509)
```text
        position[id][onBehalf].debt -= UtilsLib.toUint128(units);
        marketState[id].withdrawable += UtilsLib.toUint128(units);
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

**File:** src/Midnight.sol (L564-568)
```text
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

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
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

**File:** certora/specs/Reverts.spec (L33-34)
```text
    // For oracle rules, re-entrant callbacks cannot deactivate collaterals without calling
    // withdrawCollateral -> isHealthy which would hit the same reverting/zero oracle.
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
