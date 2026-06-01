Based on my code investigation, all key claims are verified:

1. `badDebt` is computed from a pre-callback bitmap snapshot at lines 602–618 [1](#0-0) 
2. `badDebt` is committed to storage (reducing `_position.debt`, updating `lossFactor`/`totalUnits`) at lines 626–641, **before** the callback [2](#0-1) 
3. The `onLiquidate` callback fires at lines 698–715, **after** all storage mutations [3](#0-2) 
4. No reentrancy guard exists anywhere in `src/Midnight.sol` (grep for `nonReentrant`, `ReentrancyGuard`, `_locked`, `_entered` returns zero matches)
5. The Certora spec at line 73 of `OnlyAuthorizedCanChange.spec` explicitly states the reentrancy scenario is **not covered** [4](#0-3) 

---

Audit Report

## Title
Reentrant `supplyCollateral` during `onLiquidate` callback causes `badDebt` over-realization, slashing lenders beyond actual undercollateralization - (File: src/Midnight.sol)

## Summary
In `liquidate()`, `badDebt` is computed from a pre-callback snapshot of collateral storage, then immediately committed to storage (reducing `_position.debt`, inflating `lossFactor`, reducing `totalUnits`), before the `onLiquidate` callback fires. A callback contract authorized by the borrower can reenter `supplyCollateral` during the callback, adding collateral that would have reduced or eliminated `badDebt`. Because this collateral is added after the `badDebt` computation and commitment, lenders are socialized a loss larger than the actual undercollateralization.

## Finding Description
**Root cause:** `liquidate()` follows a compute-commit-callback pattern with no reentrancy guard, and `supplyCollateral` has no reentrancy guard either.

**Step 1 — Snapshot and compute:** At lines 606–618, `liquidate()` copies `_position.collateralBitmap` into a local `uint128 _collateralBitmap`, iterates over it reading `_position.collateral[i]` from storage, and accumulates `badDebt` and `maxDebt`. [1](#0-0) 

**Step 2 — Commit to storage:** At lines 626–641, before any external call, `badDebt` is applied: `_position.debt` is reduced, `_marketState.lossFactor` is increased, and `_marketState.totalUnits` is decreased. Lenders are slashed at this point. [2](#0-1) 

**Step 3 — Callback fires:** At lines 698–715, `ILiquidateCallback(callback).onLiquidate(...)` is called. At this point all `badDebt` accounting is already finalized in storage. [3](#0-2) 

**Step 4 — Reentrant `supplyCollateral`:** Inside the callback, the callback contract (authorized by the borrower via `isAuthorized[borrower][callbackContract] == true`) calls `supplyCollateral`. There is no reentrancy guard on either `liquidate()` or `supplyCollateral()`. The call succeeds, updating `_position.collateral[newIndex]` and `_position.collateralBitmap` in storage. This collateral was never counted in the `badDebt` computation.

**Why existing checks fail:** There is no `nonReentrant` modifier or equivalent lock anywhere in `src/Midnight.sol`. The Certora formal verification explicitly excludes this scenario — `OnlyAuthorizedCanChange.spec` line 73 states: *"Assumes no reentrancy: callbacks and token transfers are not modeled as re-entering Midnight, so re-entrant collateral changes are not covered."* [4](#0-3) 

## Impact Explanation
`badDebt` is over-realized by `Δ = additionalCollateral × price / ORACLE_PRICE_SCALE × WAD / maxLif`. This excess is permanently socialized to all lenders in the market via an inflated `lossFactor` and reduced `totalUnits`. Lenders suffer a concrete credit loss larger than the actual undercollateralization. The borrower's debt is reduced by more than warranted, and the supplied collateral remains in the borrower's position and can be withdrawn once the position is healthy, making the net cost to the attacker near zero. This constitutes direct, measurable loss of lender funds.

## Likelihood Explanation
Preconditions are realistic in standard DeFi usage: borrowers routinely authorize position-management or callback contracts. The borrower and liquidator can be the same entity (a borrower authorizes their own liquidation contract). The attack requires the position to have `badDebt > 0`, which occurs whenever collateral value falls below the max-LIF-weighted debt — a normal market condition. The attack is repeatable across any market with `liquidatorGate == address(0)` (unrestricted) or where the callback contract passes the gate.

## Recommendation
Add a reentrancy guard (e.g., a `nonReentrant` modifier using a storage lock) to `liquidate()` and `supplyCollateral()`, or at minimum to `liquidate()` alone, since that is the entry point whose callback enables the attack. Alternatively, move the `onLiquidate` callback to before the `badDebt` commitment, so that any collateral supplied during the callback is visible when `badDebt` is computed. A checks-effects-interactions pattern where the callback fires before storage mutations would also resolve the issue.

## Proof of Concept
1. Deploy a `CallbackContract` that implements `onLiquidate` and calls `supplyCollateral(market, newIndex, largeAmount, borrower)` inside the callback.
2. Borrower calls `setIsAuthorized(callbackContract, true)`.
3. Drive the borrower's position to `badDebt > 0` (collateral value < max-LIF-weighted debt).
4. Call `liquidate(market, collateralIndex, 0, 0, borrower, false, receiver, callbackContract, data)`.
5. Observe: `lossFactor` and `totalUnits` reflect the full pre-callback `badDebt`, while `_position.collateral[newIndex]` now holds the additional collateral supplied during the callback.
6. Assert: the `lossFactor` increase corresponds to a `badDebt` larger than `originalDebt - Σ(collateral[i] × price[i] / ORACLE_PRICE_SCALE × WAD / maxLif[i])` computed over the final post-callback collateral state, confirming lenders were over-slashed.

### Citations

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

**File:** src/Midnight.sol (L698-715)
```text
        if (callback != address(0)) {
            require(
                ILiquidateCallback(callback)
                    .onLiquidate(
                        msg.sender,
                        id,
                        market,
                        collateralIndex,
                        seizedAssets,
                        repaidUnits,
                        borrower,
                        receiver,
                        data,
                        badDebt
                    ) == CALLBACK_SUCCESS,
                WrongLiquidateCallbackReturnValue()
            );
        }
```

**File:** certora/specs/OnlyAuthorizedCanChange.spec (L72-74)
```text
/// An unauthorized caller cannot change a user's collateral except via liquidate.
/// Assumes no reentrancy: callbacks and token transfers are not modeled as re-entering Midnight, so re-entrant collateral changes are not covered.
rule onlyAuthorizedCanChangeCollateralExceptLiquidate(env e, method f, calldataarg args, bytes32 id, address user, uint256 collateralIndex) filtered { f -> f.selector != sig:liquidate(Midnight.Market, uint256, uint256, uint256, address, bool, address, address, bytes).selector } {
```
