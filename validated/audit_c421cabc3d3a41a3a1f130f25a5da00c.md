Audit Report

## Title
`onLiquidate` Callback Enables Post-Bad-Debt Collateral Injection, Over-Slashing Lenders - (File: src/Midnight.sol)

## Summary
In `liquidate()`, bad debt is computed from a local snapshot of `_position.collateralBitmap` and immediately committed to `lossFactor`, `totalUnits`, and `_position.debt` before the `onLiquidate` callback fires. Because `supplyCollateral` has no reentrancy guard and `liquidationLocked` is never set during `liquidate()`, a borrower-authorized callback can inject new collateral into the position after bad debt accounting is finalized. The injected collateral is never counted against `badDebt`, causing lenders to be permanently over-slashed by the collateral's oracle-weighted value.

## Finding Description

**Root cause — local bitmap snapshot used for bad debt, storage mutated before callback:**

At line 606, `liquidate()` captures a local copy of the bitmap:

```solidity
uint128 _collateralBitmap = _position.collateralBitmap;   // line 606
while (_collateralBitmap != 0) { ... }                    // badDebt computed here
``` [1](#0-0) 

Bad debt is then immediately written to storage at lines 626–641 — `_position.debt`, `_marketState.lossFactor`, and `_marketState.totalUnits` are all updated — before any external call: [2](#0-1) 

The `onLiquidate` callback fires only after all accounting is finalized, at lines 698–715: [3](#0-2) 

**`supplyCollateral` is unguarded during `liquidate()`:**

`supplyCollateral` has no reentrancy guard and no `liquidationLocked` check — its only gate is the authorization check:

```solidity
require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
``` [4](#0-3) 

`liquidationLocked` is exclusively set inside `take()` (line 444), never inside `liquidate()`: [5](#0-4) 

**Exploit flow:**

1. Borrower has debt `D`, collateral `C0` at index 0; oracle price drops so `D > C0·price0/maxLif0` (bad debt territory).
2. Borrower calls `setIsAuthorized(maliciousCallback, true, borrower)`. Callback contract is pre-funded with `C1` tokens (collateral at index 1).
3. Anyone (including the borrower) calls `liquidate(market, 0, 0, 0, borrower, false, receiver, maliciousCallback, data)`.
4. Inside `liquidate()`:
   - `badDebt = D − C0·price0/maxLif0` computed from local bitmap snapshot (only index 0 present).
   - `lossFactor` updated, `totalUnits` reduced by `badDebt` — lenders are slashed.
   - `_position.debt -= badDebt`.
5. `onLiquidate` fires. `maliciousCallback` calls `supplyCollateral(market, 1, C1amount, borrower)`. Because `isAuthorized[borrower][maliciousCallback]` is true, this succeeds. `_position.collateral[1] = C1amount` and bit 1 is set in `_position.collateralBitmap`. Tokens are pulled from the callback contract.
6. Callback returns `CALLBACK_SUCCESS`; 0 loan tokens pulled (since `repaidUnits = 0`).
7. Borrower's position is now healthy (debt = `C0·price0/maxLif0`, collateral includes both C0 and C1).
8. Borrower calls `withdrawCollateral(market, 1, C1amount, borrower, borrower)` to recover the C1 tokens.

**Why existing checks fail:**

- `liquidationLocked` is never set in `liquidate()`, so `supplyCollateral` is not blocked during the callback window.
- `supplyCollateral`'s only guard is the authorization check, which the attacker satisfies by pre-authorizing the callback.
- The bad debt loop iterates over the local `_collateralBitmap` snapshot, so any collateral added to `_position.collateralBitmap` during the callback is invisible to the already-completed bad debt computation.

## Impact Explanation

Lenders are permanently over-slashed by `C1amount · price1 / ORACLE_PRICE_SCALE · WAD / maxLif1` units of credit. The borrower's debt is reduced by this same extra amount while the borrower recovers the `C1` collateral tokens (withdrawable once healthy). This is a direct, permanent reduction in lender redeemable value and violates the protocol invariant that bad debt realization must account for all collateral present at the time of liquidation. The attack is repeatable across any market with bad debt.

## Likelihood Explanation

**Preconditions:**
1. Position must be in bad debt territory — a realistic market condition requiring only an oracle price drop.
2. Borrower must have authorized the callback contract — fully attacker-controlled, as the borrower can call `setIsAuthorized` at any time before the liquidation.

Self-liquidation is not blocked anywhere in `liquidate()`. The borrower can be both the liquidator and the callback operator. No privileged role is required. The attack is executable by any normal user who controls a borrower position.

## Recommendation

Set `liquidationLocked` for the borrower during the `liquidate()` execution window (analogous to how `take()` sets it for the seller at line 444), or add a reentrancy guard to `supplyCollateral` that prevents it from being called during an active `liquidate()` callback. Alternatively, snapshot and freeze `_position.collateralBitmap` in transient storage at the start of `liquidate()` and revert if it changes before the function returns.

## Proof of Concept

**Minimal manual steps:**

1. Deploy a market with two collaterals (index 0 and index 1).
2. Borrower supplies collateral at index 0 and borrows. Drop oracle price for index 0 so the position enters bad debt territory.
3. Borrower calls `setIsAuthorized(MaliciousCallback, true, borrower)`.
4. Fund `MaliciousCallback` with collateral tokens for index 1.
5. Implement `MaliciousCallback.onLiquidate()` to call `Midnight.supplyCollateral(market, 1, largeAmount, borrower)`.
6. Call `liquidate(market, 0, 0, 0, borrower, false, receiver, MaliciousCallback, "")`.
7. Observe: `lossFactor` reflects a `badDebt` computed without index-1 collateral; borrower's debt is reduced by the over-computed amount; borrower can then call `withdrawCollateral` to recover the index-1 tokens.
8. Assert: lender's redeemable credit is less than it would have been had the index-1 collateral been present during bad debt computation.

### Citations

**File:** src/Midnight.sol (L444-475)
```text
        bool wasLocked = UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, true);
        if (buyerCallback != address(0)) {
            bytes memory buyerCallbackData = offer.buy ? offer.callbackData : takerCallbackData;
            require(
                IBuyCallback(buyerCallback)
                    .onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
                == CALLBACK_SUCCESS,
                WrongBuyCallbackReturnValue()
            );
        }

        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);

        if (sellerCallback != address(0)) {
            bytes memory sellerCallbackData = offer.buy ? takerCallbackData : offer.callbackData;
            require(
                ISellCallback(sellerCallback)
                    .onSell(
                        id,
                        offer.market,
                        sellerAssets,
                        units,
                        sellerPendingFeeDecrease,
                        seller,
                        receiver,
                        sellerCallbackData
                    ) == CALLBACK_SUCCESS,
                WrongSellCallbackReturnValue()
            );
        }
        if (!wasLocked) UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, false);
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
