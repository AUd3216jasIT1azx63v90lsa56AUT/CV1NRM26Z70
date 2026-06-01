### Title
`onLiquidate` callback can call `supplyCollateral` after bad-debt loop, causing lenders to be over-slashed - (File: src/Midnight.sol)

### Summary
In `liquidate()`, the collateral bitmap is snapshotted and `badDebt` is fully computed and applied to `lossFactor`/`totalUnits` before the `onLiquidate` callback fires. A callback that is authorized for the borrower can call `supplyCollateral` during this window, adding collateral that was never counted in the bad-debt formula. Lenders are slashed for a shortfall that is larger than the true post-liquidation shortfall.

### Finding Description
**Code path:**

`src/Midnight.sol` — `liquidate()`:

1. **Line 606** — local bitmap snapshot: `uint128 _collateralBitmap = _position.collateralBitmap;` [1](#0-0) 

2. **Lines 607–618** — `badDebt` computed from the snapshot; `_position.collateral[i]` is read from storage but only for indices already in the snapshot.

3. **Lines 626–641** — `badDebt` is immediately committed: `_position.debt -= uint128(badDebt)`, `_marketState.lossFactor` updated, `_marketState.totalUnits` reduced. Lenders are slashed here, before any external call. [2](#0-1) 

4. **Lines 698–715** — `onLiquidate` callback fires. At this point all bad-debt accounting is final. [3](#0-2) 

5. Inside the callback, `supplyCollateral` is called for `borrower`. The authorization check is `onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender]`. [4](#0-3) 

   If `callback == borrower` (borrower is a smart contract implementing `ILiquidateCallback`), then `msg.sender == borrower` satisfies the check with no additional setup. Alternatively, the borrower pre-authorizes the callback contract via `setIsAuthorized`.

6. `supplyCollateral` writes `_position.collateral[newIndex]` and sets the corresponding bit in `_position.collateralBitmap`. Neither of these writes is visible to the already-completed bad-debt loop.

7. **Line 717** — `safeTransferFrom` pulls loan tokens (0 if `repaidUnits == 0`). No re-check of collateral or bad debt occurs. [5](#0-4) 

**Why existing checks do not stop it:**
- `liquidationLocked` is a transient-storage flag set only inside `take()` (line 444), not inside `liquidate()`. It does not block `supplyCollateral` from being called during the `onLiquidate` callback. [6](#0-5) 
- There is no reentrancy guard on `liquidate()` or `supplyCollateral()`.
- The Certora `CollateralBitmap.spec` invariant `nonZeroCollateralsAreActivated` is proved only for normal function calls; it does not constrain the mid-execution state during a callback. [7](#0-6) 

**Attacker-controlled inputs:**
- `callback` address (set to a contract the attacker controls and that is authorized for `borrower`)
- `newIndex` and `largeAmount` passed to `supplyCollateral` inside the callback
- `seizedAssets = 0`, `repaidUnits = 0` (pure bad-debt realization, no loan-token pull required)

### Impact Explanation
`badDebt` realized = `D − Σ(collateral_i · price_i / maxLif_i)` (pre-callback collateral only).  
True shortfall after callback = `D − Σ(collateral_i · price_i / maxLif_i) − A · price_new / maxLif_new`.  
Over-slashing per attack = `A · price_new / maxLif_new` units, subtracted from `totalUnits` and added to `lossFactor`. Every lender's effective credit is permanently reduced by this excess amount when they call `updatePosition`.

### Likelihood Explanation
**Preconditions:**
1. Borrower is a smart contract (or has pre-authorized the callback) — realistic for any protocol-integrated borrower.
2. Position has genuine bad debt (oracle price drop) — the normal trigger for bad-debt realization.
3. Callback contract holds collateral tokens to supply — can be sourced from a flash loan within the same callback, since `supplyCollateral` pulls from `msg.sender` (the callback) and no repayment is required within `liquidate()`.

The attack is repeatable on any market with bad debt and a callback-capable borrower. Self-liquidation (borrower == liquidator) requires no third-party cooperation.

### Recommendation
Snapshot the full collateral state (or re-compute bad debt) **after** the callback, or prohibit collateral-increasing re-entrant calls during `liquidate()` by setting a transient lock (analogous to `LIQUIDATION_LOCK_SLOT` used in `take()`) before the bad-debt loop and clearing it only after the callback returns. The simplest targeted fix is to move the `onLiquidate` callback invocation to **before** the bad-debt computation and application block, so any collateral added by the callback is included in the `badDebt` formula.

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

// Foundry unit test outline
contract MaliciousBorrower is ILiquidateCallback {
    Midnight midnight;
    Market market;
    uint256 newIndex;
    uint256 supplyAmount;
    IERC20 newToken;

    function setup(Midnight _m, Market memory _mkt, uint256 _idx, uint256 _amt, IERC20 _tok) external {
        midnight = _m; market = _mkt; newIndex = _idx; supplyAmount = _amt; newToken = _tok;
    }

    function onLiquidate(
        address, bytes32, Market memory, uint256, uint256, uint256,
        address, address, bytes memory, uint256
    ) external returns (bytes32) {
        // msg.sender == Midnight, onBehalf == address(this) == borrower → authorized
        newToken.approve(address(midnight), supplyAmount);
        midnight.supplyCollateral(market, newIndex, supplyAmount, address(this));
        return CALLBACK_SUCCESS;
    }
}

function testBadDebtOverSlash() public {
    // 1. Deploy MaliciousBorrower, fund with debt and collateral[0]
    // 2. Drop oracle price so badDebt > 0 without callback
    uint256 trueBadDebt = computeBadDebt(); // debt - collateral[0]*price/maxLif
    // 3. Fund MaliciousBorrower with collateral[1] tokens for supplyCollateral
    // 4. Call liquidate(market, 0, 0, 0, borrower, false, receiver, callback=borrower, "")
    uint256 lossFactorBefore = midnight.lossFactor(id);
    midnight.liquidate(market, 0, 0, 0, address(borrower), false, address(this), address(borrower), "");
    uint256 lossFactorAfter = midnight.lossFactor(id);

    // 5. Assert: realized badDebt > trueBadDebt
    // lossFactor increased by more than trueBadDebt warrants
    uint256 realizedBadDebt = computeRealizedBadDebtFromLossFactor(lossFactorBefore, lossFactorAfter);
    assertGt(realizedBadDebt, trueBadDebt, "lenders over-slashed");

    // 6. Assert: borrower still has collateral[1] in position (not counted in badDebt)
    assertGt(midnight.collateral(id, address(borrower), 1), 0, "new collateral present post-liquidation");

    // 7. Assert: badDebt == max(0, debt - true_collateral_value_after_callback) is violated
    uint256 trueShortfallAfterCallback = computeTrueShortfall(); // includes collateral[1]
    assertGt(realizedBadDebt, trueShortfallAfterCallback, "invariant violated");
}
```

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

**File:** src/Midnight.sol (L717-719)
```text
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);

        return (seizedAssets, repaidUnits);
```

**File:** src/Midnight.sol (L937-939)
```text
    function liquidationLocked(bytes32 id, address user) public view returns (bool) {
        return UtilsLib.tGet(LIQUIDATION_LOCK_SLOT, id, user);
    }
```

**File:** certora/specs/CollateralBitmap.spec (L39-45)
```text
strong invariant nonZeroCollateralsAreActivated(bytes32 id, address user, uint256 collateralIndex)
    collateralIndex < 128 => (collateral(id, user, collateralIndex) != 0 <=> summaryGetBit(currentContract.position[id][user].collateralBitmap, collateralIndex));

// Check that the number of activated collaterals never exceeds MAX_COLLATERALS_PER_BORROWER.
// This bounds the while-loop iterations in isHealthy() and liquidate().
strong invariant atMostMaxCollateralsBitsSet(bytes32 id, address user)
    summaryCountBits(currentContract.position[id][user].collateralBitmap) <= MAX_COLLATERALS_PER_BORROWER();
```
