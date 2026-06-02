After verifying all code references against `src/Midnight.sol`:

**All claims check out against the actual code.** Here is the validated output:

---

Audit Report

## Title
`onLiquidate` Callback Enables Post-Bad-Debt Collateral Injection, Over-Slashing Lenders - (File: src/Midnight.sol)

## Summary
In `liquidate()`, bad debt is computed from a local snapshot of `_position.collateralBitmap` and immediately committed to `lossFactor`, `totalUnits`, and `_position.debt` before the `onLiquidate` callback fires. Because `supplyCollateral` has no reentrancy guard and `liquidationLocked` is never set during `liquidate()`, a borrower-authorized callback can inject new collateral into the position after bad debt accounting is finalized. The injected collateral is never counted against `badDebt`, causing lenders to be permanently over-slashed by the collateral's oracle-weighted value.

## Finding Description

**Root cause — local bitmap snapshot used for bad debt, storage mutated before callback:**

At line 606, `liquidate()` captures a local copy of the bitmap: [1](#0-0) 

Bad debt is then immediately written to storage at lines 626–641 — `_position.debt`, `_marketState.lossFactor`, and `_marketState.totalUnits` are all updated — before any external call: [2](#0-1) 

The `onLiquidate` callback fires only after all accounting is finalized, at lines 698–715: [3](#0-2) 

**`supplyCollateral` is unguarded during `liquidate()`:**

`supplyCollateral` has no reentrancy guard and no `liquidationLocked` check — its only gate is the authorization check: [4](#0-3) 

`liquidationLocked` is exclusively set inside `take()` (line 444), never inside `liquidate()`: [5](#0-4) 

The `liquidate()` function only *reads* `liquidationLocked` as a precondition check (line 621) but never sets it, leaving the lock absent during the callback window: [6](#0-5) 

**Exploit flow:**

1. Borrower has debt `D`, collateral `C0` at index 0; oracle price drops so `D > C0·price0/maxLif0` (bad debt territory).
2. Borrower calls `setIsAuthorized(maliciousCallback, true, borrower)`.
3. Anyone (including the borrower) calls `liquidate(market, 0, 0, 0, borrower, false, receiver, maliciousCallback, data)`.
4. Inside `liquidate()`: `badDebt = D − C0·price0/maxLif0` computed from local bitmap snapshot (only index 0 present). `lossFactor` updated, `totalUnits` reduced — lenders are slashed. `_position.debt -= badDebt`.
5. `onLiquidate` fires. `maliciousCallback` calls `supplyCollateral(market, 1, largeAmount, borrower)`. Because `isAuthorized[borrower][maliciousCallback]` is true, this succeeds. `_position.collateral[1] = largeAmount` and bit 1 is set in `_position.collateralBitmap`.
6. Callback returns `CALLBACK_SUCCESS`; 0 loan tokens pulled (since `repaidUnits = 0`).
7. Borrower's debt is now 0 (or near 0); position is healthy. Borrower calls `withdrawCollateral` to recover the injected `C1` tokens.

**Why existing checks fail:**

- `liquidationLocked` is never set in `liquidate()`, so `supplyCollateral` is not blocked during the callback window.
- `supplyCollateral`'s only guard is the authorization check, which the attacker satisfies by pre-authorizing the callback.
- The bad debt loop iterates over the local `_collateralBitmap` snapshot, so any collateral added to `_position.collateralBitmap` during the callback is invisible to the already-completed bad debt computation.
- No health re-check or position re-validation occurs after the callback (line 717 only pulls loan tokens). [7](#0-6) 

## Impact Explanation

Lenders are permanently over-slashed by `C1·price1/ORACLE_PRICE_SCALE·WAD/maxLif1` units of credit — the oracle-weighted value of the injected collateral. The borrower's debt is reduced by this same extra amount while the borrower retains the `C1` collateral tokens in their position (withdrawable once healthy). This is a direct, permanent reduction in lender redeemable value and violates the protocol invariant that bad debt realization must account for all collateral present at the time of liquidation. The attack is repeatable across any market with bad debt.

## Likelihood Explanation

**Preconditions:**
1. Position must be in bad debt territory — a realistic market condition requiring only an oracle price drop.
2. Borrower must have authorized the callback contract — fully attacker-controlled, as the borrower can call `setIsAuthorized` at any time before the liquidation.

Self-liquidation is not blocked anywhere in `liquidate()`. The borrower can be both the liquidator and the callback operator. No privileged role is required. The attack is executable by any normal user who controls a borrower position.

## Recommendation

Set `liquidationLocked` for the borrower at the start of `liquidate()` (and clear it at the end), mirroring the pattern used in `take()` at line 444. This prevents `supplyCollateral` (and other position-mutating functions that respect the lock) from being called during the liquidation callback window. Alternatively, re-compute bad debt from `_position.collateralBitmap` (storage, not a local snapshot) after the callback returns, and apply any delta to `lossFactor`/`totalUnits` accordingly.

## Proof of Concept

**Minimal manual steps:**

1. Deploy a market with two collateral tokens (C0, C1) and a loan token.
2. Borrower supplies C0, borrows D such that `D > C0·price0/maxLif0` after oracle price drop.
3. Borrower deploys `MaliciousCallback` that, in `onLiquidate`, calls `supplyCollateral(market, 1, largeAmount, borrower)` (pre-funded with C1 tokens, pre-approved to the protocol).
4. Borrower calls `setIsAuthorized(MaliciousCallback, true, borrower)`.
5. Call `liquidate(market, 0, 0, 0, borrower, false, receiver, MaliciousCallback, "")`.
6. Assert: `marketState[id].lossFactor` reflects a larger bad debt than `D − C0·price0/maxLif0 − C1·price1/maxLif1`.
7. Borrower calls `withdrawCollateral(market, 1, largeAmount, borrower, borrower)` — succeeds, recovering C1.
8. Lenders' redeemable credit is permanently reduced by the extra slash amount.

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

**File:** src/Midnight.sol (L717-719)
```text
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);

        return (seizedAssets, repaidUnits);
```
