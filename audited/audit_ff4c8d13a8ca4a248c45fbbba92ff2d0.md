### Title
Bad Debt Over-Realization via `onLiquidate` Callback Supplying Collateral After Bitmap Snapshot - (`src/Midnight.sol`)

### Summary
The `liquidate` function snapshots `_position.collateralBitmap` into a local variable before computing `badDebt`, then fully realizes that bad debt (modifying `_position.debt` and `_marketState.lossFactor`) before invoking the `onLiquidate` callback. A borrower-controlled callback can call `supplyCollateral` for the borrower at a previously-inactive index during the callback, setting a new bit in the storage bitmap and depositing real collateral value — but since `badDebt` was already computed and applied from the pre-callback snapshot, that collateral is never subtracted from `badDebt`, causing the protocol to over-realize bad debt and over-slash lenders.

### Finding Description

**Exact code path:**

`src/Midnight.sol` line 606 — local bitmap snapshot: [1](#0-0) 

`badDebt` is computed entirely from this local snapshot. `_position.collateral[i]` is read from storage, but only for indices present in the snapshot. Any index not yet in the bitmap at snapshot time is invisible to the loop.

Lines 626–641 — bad debt is **fully realized** before any external call: [2](#0-1) 

`_position.debt` is reduced by `badDebt`, `_marketState.lossFactor` is ratcheted up, and `_marketState.totalUnits` is reduced — all irreversibly, before the callback.

Lines 698–715 — `onLiquidate` callback is invoked after realization: [3](#0-2) 

**`supplyCollateral` authorization:** [4](#0-3) 

The check is `onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender]`. If the borrower is a smart contract that passes `callback = address(this)` to `liquidate`, then inside `onLiquidate` it calls `supplyCollateral(market, newIndex, assets, address(this))`. Here `onBehalf == msg.sender` (both are the borrower contract), so the authorization check passes with zero pre-conditions.

**Exploit flow:**

1. Borrower is a smart contract with a position where all existing collateral is worth less than debt at `maxLif` prices (genuine bad debt condition).
2. Borrower calls `liquidate(market, 0, 0, 0, address(this), false, receiver, address(this), "")` — `seizedAssets = repaidUnits = 0` (pure bad-debt realization), `callback = address(this)`.
3. `liquidate` snapshots `_collateralBitmap` (line 606), loops over existing collateral, computes `badDebt = originalDebt - sum(collateral_i * price_i / maxLif_i)` (a positive value).
4. `liquidate` applies `badDebt`: reduces `_position.debt`, increases `_marketState.lossFactor`, reduces `_marketState.totalUnits` (lines 626–641).
5. `liquidate` calls `onLiquidate` on the borrower contract (line 700).
6. Inside `onLiquidate`, the borrower contract calls `supplyCollateral(market, newIndex, largeAmount, address(this))`. This sets `_position.collateral[newIndex]` and sets the corresponding bit in `_position.collateralBitmap` in storage.
7. `liquidate` returns. The new collateral is now in the position, but `badDebt` was already applied without it.

**Why existing checks fail:**

- No reentrancy guard on `liquidate` or `supplyCollateral`.
- No post-callback re-computation or validation of `badDebt`.
- The `_collateralBitmap` local variable is never refreshed from storage after the callback.
- `supplyCollateral`'s authorization check is satisfied trivially when the borrower is its own callback.

### Impact Explanation

`_marketState.lossFactor` is set higher than it should be, and `_marketState.totalUnits` is reduced by more than warranted. When any lender subsequently calls `updatePosition`, their credit is slashed proportionally to the inflated loss factor. The borrower's debt is reduced by a `badDebt` amount that exceeds the true uncollateralized shortfall — the borrower effectively has debt forgiven that was actually backed by the newly supplied collateral. Lenders bear a loss they should not bear.

### Likelihood Explanation

Preconditions: (1) a position must have genuine bad debt (collateral value < debt at `maxLif` prices), which is a normal liquidation scenario; (2) the borrower must be a smart contract — common in DeFi; (3) the borrower passes itself as the callback — no external authorization required. The attack is repeatable on any market with bad debt and requires no privileged access, no oracle manipulation, and no token owner cooperation.

### Recommendation

Move the `onLiquidate` callback invocation to **before** bad debt computation and realization, or re-snapshot `_position.collateralBitmap` from storage after the callback and recompute `badDebt` before applying it. The simplest safe fix is to add a reentrancy lock (e.g., a per-market or global `nonReentrant` modifier) that prevents `supplyCollateral` (and other state-mutating functions) from being called while `liquidate` is executing. Alternatively, apply bad debt realization only after the callback, using the post-callback storage state.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

// Foundry unit test plan:
//
// contract BorrowerCallback is ILiquidateCallback {
//     IMidnight midnight;
//     Market market;
//     uint256 newIndex;       // a collateral index not yet active for this borrower
//     address newToken;
//     uint256 newAmount;      // large enough to cover the full debt at maxLif price
//
//     function onLiquidate(..., uint256 badDebt) external returns (bytes32) {
//         // Supply collateral at newIndex for ourselves during the callback
//         IERC20(newToken).approve(address(midnight), newAmount);
//         midnight.supplyCollateral(market, newIndex, newAmount, address(this));
//         return CALLBACK_SUCCESS;
//     }
//
//     function attack() external {
//         // Step 1: set up position with debt and collateral[0] at bad-debt price
//         // Step 2: record lender credit before
//         uint128 lenderCreditBefore = midnight.creditOf(id, lender);
//
//         // Step 3: call liquidate with seizedAssets=0, repaidUnits=0, callback=address(this)
//         midnight.liquidate(market, 0, 0, 0, address(this), false, address(this), address(this), "");
//
//         // Step 4: update lender position to realize slashing
//         midnight.updatePosition(market, lender);
//         uint128 lenderCreditAfter = midnight.creditOf(id, lender);
//
//         // Step 5: compute what badDebt SHOULD have been (with newIndex collateral counted)
//         // expectedBadDebt = max(0, debt - collateral[0]*price0/maxLif0 - newAmount*priceNew/maxLifNew)
//         // which should be 0 or much smaller than what was actually applied
//
//         // Assertions:
//         // assert lenderCreditAfter < lenderCreditBefore  (lenders were slashed)
//         // assert actualBadDebt > expectedBadDebt          (over-realization confirmed)
//         // assert midnight.collateral(id, address(this), newIndex) == newAmount (new collateral present)
//         // assert midnight.lossFactor(id) > expectedLossFactor (inflated loss factor)
//     }
// }
```

The test asserts that `lossFactor` after the attack exceeds the value it would have if `badDebt` had been computed with the newly supplied collateral included, and that lenders' credit is reduced by more than the true uncollateralized shortfall warrants.

### Citations

**File:** src/Midnight.sol (L527-541)
```text
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
