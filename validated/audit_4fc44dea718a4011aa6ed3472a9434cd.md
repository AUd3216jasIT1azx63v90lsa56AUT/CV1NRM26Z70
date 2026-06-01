Audit Report

## Title
Bad Debt Over-Realization via `supplyCollateral` Reentrancy in `onLiquidate` Callback - (File: src/Midnight.sol)

## Summary

`liquidate()` computes `badDebt` from a local bitmap snapshot and commits it to storage — slashing lenders via `_marketState.lossFactor` — before invoking the `onLiquidate` external callback. Because no reentrancy guard exists on `liquidate()`, an authorized callback can call `supplyCollateral` for the borrower mid-execution, adding collateral that was absent from the snapshot. Lenders are permanently slashed by a `badDebt` that exceeds the true undercollateralization at callback-completion time, while the attacker recovers the supplied collateral at negligible net cost.

## Finding Description

**Exact code path in `src/Midnight.sol`:**

- **Line 606**: Local bitmap snapshot taken: `uint128 _collateralBitmap = _position.collateralBitmap;` [1](#0-0) 

- **Lines 607–618**: `maxDebt` and `badDebt` computed by iterating only over bits present in the snapshot. Collateral added after this point is invisible to the computation. [2](#0-1) 

- **Lines 626–641**: `badDebt` irreversibly written to storage — `_position.debt -= uint128(badDebt)` and `_marketState.lossFactor` updated (lenders slashed). [3](#0-2) 

- **Lines 698–715**: `ILiquidateCallback(callback).onLiquidate(...)` is called **after** the bad-debt write. No reentrancy lock is set before this call. [4](#0-3) 

- **Line 444**: The `liquidationLocked` / `tExchange` mechanism is set exclusively inside `take()`, never inside `liquidate()`. [5](#0-4) 

- **Lines 524–546**: `supplyCollateral` only checks `isAuthorized[onBehalf][msg.sender]`. It sets a new bit in `_position.collateralBitmap` and increases `_position.collateral[newIndex]` with no reentrancy guard. [6](#0-5) 

**Root cause:** The protocol assumes that the collateral state is frozen between the `badDebt` computation and the external callback. This assumption is broken because `supplyCollateral` is callable by any authorized address at any time, including during the `onLiquidate` callback, and `liquidate()` has no reentrancy protection.

**Exploit flow:**

1. Borrower (= attacker) holds debt `D`, collateral `C₀` at index 0 only. Position has genuine bad debt: `D > C₀ · price₀ / ORACLE_PRICE_SCALE · lltv₀ / WAD`.
2. Attacker calls `setIsAuthorized(callbackContract, true, borrower)`.
3. Attacker calls `liquidate(market, 0, 0, 0, borrower, false, receiver, callbackContract, data)`.
4. `badDebt = D − C₀ · price₀ / ORACLE_PRICE_SCALE · WAD / maxLif₀` computed from snapshot (index 0 only).
5. `_position.debt -= badDebt`; `_marketState.lossFactor` updated — lenders slashed by `badDebt`.
6. `onLiquidate` fires. Callback calls `supplyCollateral(market, 1, largeAmount, borrower)`. Bit 1 set in `_position.collateralBitmap`; `_position.collateral[1] = largeAmount`. This collateral was absent from the snapshot and therefore not subtracted from `badDebt`.
7. Callback returns `CALLBACK_SUCCESS`. Zero loan tokens pulled (`repaidUnits = 0`).
8. Post-liquidation: position debt = `C₀ · price₀ / ORACLE_PRICE_SCALE · WAD / maxLif₀`; collateral[1] = `largeAmount`.
9. Attacker repays a small residual amount (or partially withdraws collateral[1] while keeping the position healthy per the `isHealthy` check at line 568), recovering the bulk of `largeAmount`. [7](#0-6) 

**Why existing checks fail:** The `liquidationLocked` guard (line 621) checks whether the *borrower* is locked during a `take()` call — it is not a reentrancy guard on `liquidate()` itself and is never set during `liquidate()`. `supplyCollateral` has no awareness of an in-progress liquidation. [8](#0-7) 

## Impact Explanation

Lenders suffer a permanent, unwarranted credit loss. `_marketState.lossFactor` is updated with a `badDebt` value that is larger than the true undercollateralization at the time the callback completes. The difference — `largeAmount · price₁ / ORACLE_PRICE_SCALE · WAD / maxLif₁` loan-token units — is the excess slashing. Because the attacker recovers the supplied collateral (net cost ≈ gas), the attack can be repeated across every bad-debt liquidation in every multi-collateral market, compounding lender losses. The loss is irreversible: `lossFactor` is monotonically non-decreasing.

## Likelihood Explanation

All preconditions are satisfiable by a single actor who is simultaneously borrower and liquidator (self-liquidation is explicitly supported per the line 577 comment). No oracle manipulation, governance action, or victim mistake is required. The attack is applicable to any multi-collateral market with at least one unused collateral index for the borrower, which is the common case. It is repeatable and permissionless given the self-authorization path. [9](#0-8) 

## Recommendation

Apply a reentrancy lock inside `liquidate()` before any state-mutating operations, analogous to the `tExchange` pattern already used in `take()`. Specifically, set a transient-storage lock keyed on `(id, borrower)` at the start of `liquidate()` and clear it at the end. Additionally, consider moving the `onLiquidate` callback invocation to **before** the `badDebt` write, or re-computing `badDebt` after the callback returns (using the post-callback collateral state) before committing it to storage.

## Proof of Concept

**Minimal manual steps:**

1. Deploy a two-collateral market (tokens T0, T1).
2. Borrower supplies `C₀` of T0, borrows `D` loan tokens such that the position has bad debt (`D > C₀ · price₀ / ORACLE_PRICE_SCALE · lltv₀ / WAD`).
3. Borrower calls `setIsAuthorized(attackerCallback, true, borrower)`.
4. Deploy `attackerCallback` implementing `onLiquidate`: inside the callback, call `supplyCollateral(market, 1, largeAmount, borrower)` (attacker must hold `largeAmount` of T1 or use a flash loan sourced before the outer call).
5. Call `liquidate(market, 0, 0, 0, borrower, false, receiver, attackerCallback, "")`.
6. Observe: `_marketState.lossFactor` is updated with the inflated `badDebt` (computed without T1 collateral). `_position.collateral[1] = largeAmount`.
7. Call `withdrawCollateral(market, 1, largeAmount − ε, borrower, attacker)` — passes `isHealthy` because the remaining debt is covered by C₀ and the residual T1 collateral.
8. Assert: lenders' effective credit is reduced by more than the true undercollateralization; attacker's net token outflow ≈ 0.

### Citations

**File:** src/Midnight.sol (L444-444)
```text
        bool wasLocked = UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, true);
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

**File:** src/Midnight.sol (L568-568)
```text
        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());
```

**File:** src/Midnight.sol (L575-578)
```text
    /// @dev See LIQUIDATIONS section for more details.
    /// @dev At least one of seizedAssets or repaidUnits should be equal to zero.
    /// @dev Passing both 0 for seizedAssets and repaidUnits allows to realize bad debt with 0 token transferred.
    /// @dev Liquidations with both 0 for seizedAssets and repaidUnits can be done with a collateral that is not
```

**File:** src/Midnight.sol (L606-606)
```text
        uint128 _collateralBitmap = _position.collateralBitmap;
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
