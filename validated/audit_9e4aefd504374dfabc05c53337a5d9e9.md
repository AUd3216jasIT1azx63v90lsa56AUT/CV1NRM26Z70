Audit Report

## Title
`onLiquidate` Callback Enables Post-Bad-Debt Collateral Injection, Over-Slashing Lenders - (File: src/Midnight.sol)

## Summary
In `liquidate()`, bad debt is computed from a local snapshot of `_position.collateralBitmap` and immediately committed to `lossFactor`, `totalUnits`, and `_position.debt` before the `onLiquidate` callback fires. Because `supplyCollateral` has no reentrancy guard and `liquidationLocked` is never set during `liquidate()`, a borrower-authorized callback can inject new collateral into the position after bad debt accounting is finalized. The injected collateral is never counted against `badDebt`, causing lenders to be permanently over-slashed by the collateral's oracle-weighted value.

## Finding Description

**Root cause — local bitmap snapshot used for bad debt, storage mutated before callback:**

At line 606, `liquidate()` captures a local copy of the bitmap: [1](#0-0) 

The local `_collateralBitmap` is iterated to compute `badDebt` and `maxDebt`. Bad debt is then immediately written to storage at lines 626–641 — `_position.debt`, `_marketState.lossFactor`, `_marketState.totalUnits`, and `_marketState.continuousFeeCredit` are all updated — before any external call: [2](#0-1) 

The `onLiquidate` callback fires only after all accounting is finalized, at lines 698–715: [3](#0-2) 

**`supplyCollateral` is unguarded during `liquidate()`:**

`supplyCollateral` has no reentrancy guard and no `liquidationLocked` check — its only gate is the authorization check: [4](#0-3) 

`liquidationLocked` is exclusively set inside `take()` via `tExchange` at line 444, never inside `liquidate()`: [5](#0-4) 

**Exploit flow:**

1. Borrower has debt `D`, collateral `C0` at index 0; oracle price drops so `D > C0·price0/maxLif0` (bad debt territory).
2. Borrower calls `setIsAuthorized(maliciousCallback, true, borrower)`. Callback contract is pre-funded with `C1` tokens (collateral at index 1).
3. Anyone (including the borrower) calls `liquidate(market, 0, 0, 0, borrower, false, receiver, maliciousCallback, data)`. Both `seizedAssets=0` and `repaidUnits=0` pass the `atMostOneNonZero` check at line 595, which is an explicitly supported code path per the NatSpec at line 577.
4. Inside `liquidate()`:
   - `badDebt = D − C0·price0/maxLif0` computed from local bitmap snapshot (only index 0 present).
   - `lossFactor` updated, `totalUnits` reduced by `badDebt` — lenders are slashed.
   - `_position.debt -= badDebt`.
5. `onLiquidate` fires. `maliciousCallback` calls `supplyCollateral(market, 1, C1amount, borrower)`. Because `isAuthorized[borrower][maliciousCallback]` is true, this succeeds. `_position.collateral[1] = C1amount` and bit 1 is set in `_position.collateralBitmap`. Tokens are pulled from the callback contract.
6. Callback returns `CALLBACK_SUCCESS`; 0 loan tokens pulled (since `repaidUnits = 0`) at line 717.
7. Borrower's position now has debt `= C0·price0/maxLif0` and collateral includes both C0 and C1.
8. Borrower calls `withdrawCollateral(market, 1, C1amount, borrower, borrower)` to recover C1 tokens, subject only to the `isHealthy` check — which passes because C1 provides sufficient collateral backing.

**Why existing checks fail:**

- `liquidationLocked` is never set in `liquidate()`, so `supplyCollateral` is not blocked during the callback window.
- `supplyCollateral`'s only guard is the authorization check, which the attacker satisfies by pre-authorizing the callback.
- The bad debt loop iterates over the local `_collateralBitmap` snapshot, so any collateral added to `_position.collateralBitmap` during the callback is invisible to the already-completed bad debt computation.

## Impact Explanation

Lenders are permanently over-slashed by the oracle-weighted value of the injected C1 collateral. The borrower's debt is reduced by `badDebt` computed without C1, while the borrower recovers C1 tokens (withdrawable once the position is healthy with C1 present). This is a direct, permanent reduction in lender redeemable value and violates the protocol invariant that bad debt realization must account for all collateral present at the time of liquidation. The attack is repeatable across any market with bad debt.

## Likelihood Explanation

**Preconditions:**
1. Position must be in bad debt territory — a realistic market condition requiring only an oracle price drop.
2. Borrower must have authorized the callback contract — fully attacker-controlled, as the borrower can call `setIsAuthorized` at any time before the liquidation.

Self-liquidation is not blocked anywhere in `liquidate()`. The borrower can be both the liquidator and the callback operator. No privileged role is required. The attack is executable by any normal user who controls a borrower position.

## Recommendation

Set `liquidationLocked` for the borrower during the execution of `liquidate()`, analogous to how `take()` sets it for the seller at line 444. This prevents `supplyCollateral` (and other position-mutating functions that check `liquidationLocked`) from being called during the liquidation callback window. Alternatively, add an explicit `liquidationLocked` check to `supplyCollateral`, or snapshot and re-validate the collateral bitmap after the callback returns to ensure no new collateral was injected.

## Proof of Concept

1. Deploy a market with two collaterals (C0 at index 0, C1 at index 1).
2. Borrower supplies C0 and borrows `D` such that `D > C0·price0/maxLif0` after an oracle price drop (bad debt territory).
3. Borrower deploys `MaliciousCallback` pre-funded with C1 tokens, and calls `setIsAuthorized(MaliciousCallback, true, borrower)`.
4. `MaliciousCallback.onLiquidate(...)` calls `midnight.supplyCollateral(market, 1, C1amount, borrower)` and returns `CALLBACK_SUCCESS`.
5. Call `liquidate(market, 0, 0, 0, borrower, false, receiver, MaliciousCallback, "")`.
6. Assert: `marketState[id].lossFactor` increased (lenders slashed) by `badDebt` computed without C1.
7. Assert: `position[id][borrower].collateral[1] == C1amount` (C1 injected post-accounting).
8. Call `withdrawCollateral(market, 1, C1amount, borrower, borrower)` — succeeds because position is healthy with C1.
9. Assert: borrower recovered C1 tokens while lenders were over-slashed.

### Citations

**File:** src/Midnight.sol (L444-444)
```text
        bool wasLocked = UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, true);
```

**File:** src/Midnight.sol (L524-527)
```text
    function supplyCollateral(Market memory market, uint256 collateralIndex, uint256 assets, address onBehalf)
        external
    {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L605-618)
```text
        uint256 badDebt = originalDebt;
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
