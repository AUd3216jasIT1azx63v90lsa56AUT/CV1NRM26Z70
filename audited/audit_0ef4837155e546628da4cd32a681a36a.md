### Title
Integer Underflow in `withdraw` After In-Function Bad-Debt Slash — (`src/Midnight.sol`)

---

### Summary

The `withdraw` function in `Midnight.sol` calls `_updatePosition` internally before performing the credit subtraction. `_updatePosition` can silently reduce `_position.credit` by applying the current `lossFactor` (bad-debt socialization). If the caller supplied `units` equal to their pre-slash credit (read off-chain), the subsequent `_position.credit -= units` underflows and reverts with a raw arithmetic panic, giving no actionable error. The same class of issue exists in `repay` and `withdrawCollateral` via race conditions with partial liquidations.

---

### Finding Description

**Root cause — `withdraw` (primary)**

`_updatePosition` is called at line 485 *inside* `withdraw`. It computes `newCredit` by applying the market's accumulated `lossFactor`: [1](#0-0) 

It then writes the slashed value back to storage: [2](#0-1) 

Immediately after, the function subtracts the caller-supplied `units` from the now-reduced credit with no bounds check: [3](#0-2) 

If `units > newCredit` (because bad debt was realized after the user read their credit off-chain), both the `pendingFee` proportional subtraction at line 490 and the `credit` subtraction at line 493 underflow, reverting with `Arithmetic operation underflowed or overflowed outside of an unchecked block`.

**Secondary instances (race-condition underflows)**

`repay` subtracts debt with no guard and no `_updatePosition` call: [4](#0-3) 

A partial liquidation between the user's off-chain debt read and the `repay` call leaves `position.debt` smaller than `units`, causing underflow.

`withdrawCollateral` subtracts collateral with no guard: [5](#0-4) 

A partial liquidation between the user's off-chain collateral read and the `withdrawCollateral` call causes the same underflow.

**How bad debt reaches `withdraw`**

`liquidate` increases `lossFactor` when `badDebt > 0`: [6](#0-5) 

Any subsequent call to `withdraw` by a lender in that market triggers `_updatePosition`, which applies the new `lossFactor` and silently reduces `_position.credit` before the unchecked subtraction.

---

### Impact Explanation

A lender who reads their credit via `creditOf` or `position` off-chain and then calls `withdraw(market, readCredit, ...)` will have their transaction revert with an opaque arithmetic panic whenever bad debt has been realized since their read. The user's funds are not lost (they can retry with the correct amount from `updatePositionView`), but the transaction fails unexpectedly with no protocol-level error message. In automated systems or smart-contract integrations that pass a cached credit value, this becomes a reliable DoS on the withdrawal path until the integration is updated to pre-call `updatePositionView`.

**Impact: Medium** — funds not stolen, but withdrawal is blocked until the caller discovers the correct post-slash amount.

---

### Likelihood Explanation

Bad debt is a normal protocol event (any underwater borrower triggers it via `liquidate`). Markets with volatile collateral or low LLTV will realize bad debt regularly. Any lender who does not call `updatePositionView` before `withdraw` — including all off-chain tooling that reads `position.credit` directly — will hit this revert. The `withdraw` case is deterministic (not a race condition): `_updatePosition` is called *inside* the same transaction, so the slash always happens before the subtraction.

**Likelihood: Medium** — triggered by any bad-debt event in a market the lender participates in.

---

### Recommendation

After `_updatePosition` returns the new credit, cap `units` or add an explicit bounds check before the subtraction:

```solidity
// In withdraw(), after _updatePosition:
require(units <= _position.credit, InsufficientCredit());
```

Alternatively, expose a `withdrawMax` variant that reads `newCredit` from `_updatePosition`'s return value and uses that as the withdrawal amount. The same pattern should be applied to `repay` (check `units <= position[id][onBehalf].debt`) and `withdrawCollateral` (check `assets <= _position.collateral[collateralIndex]`) to give actionable errors instead of arithmetic panics.

---

### Proof of Concept

1. Market is created; lender supplies 1000 loan tokens and receives 1000 credit units (`position.credit = 1000`).
2. A borrower takes a loan; the borrower's collateral drops in value; a liquidator calls `liquidate`, realizing bad debt. `lossFactor` increases; `totalUnits` decreases by `badDebt`.
3. Lender reads `creditOf(id, lender)` off-chain → still returns the stale `1000` (storage not yet updated for this lender).
4. Lender calls `withdraw(market, 1000, lender, receiver)`.
5. Inside `withdraw`, `_updatePosition` is called at line 485. It computes `newCredit = 900` (10% slashed) and writes `_position.credit = 900`.
6. Line 490: `pendingFeeDecrease = pendingFee * 1000 / 900` → exceeds `pendingFee` → `_position.pendingFee -= pendingFeeDecrease` underflows → **revert**.
7. Even if `pendingFee == 0`, line 493: `900 - 1000` underflows → **revert**.
8. The lender's withdrawal fails with `Arithmetic operation underflowed or overflowed outside of an unchecked block`. No funds are moved; the lender must discover the correct amount via `updatePositionView` and retry. [7](#0-6) [8](#0-7)

### Citations

**File:** src/Midnight.sol (L481-500)
```text
    function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        MarketState storage _marketState = marketState[id];
        _updatePosition(market, id, onBehalf);

        Position storage _position = position[id][onBehalf];
        uint128 pendingFeeDecrease;
        if (_position.credit > 0) {
            pendingFeeDecrease = UtilsLib.toUint128(_position.pendingFee.mulDivUp(units, _position.credit));
            _position.pendingFee -= pendingFeeDecrease;
        }
        _position.credit -= UtilsLib.toUint128(units);
        _marketState.withdrawable -= UtilsLib.toUint128(units);
        _marketState.totalUnits -= UtilsLib.toUint128(units);

        emit EventsLib.Withdraw(msg.sender, id, units, onBehalf, receiver, pendingFeeDecrease);

        SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
    }
```

**File:** src/Midnight.sol (L508-508)
```text
        position[id][onBehalf].debt -= UtilsLib.toUint128(units);
```

**File:** src/Midnight.sol (L561-562)
```text
        uint256 newCollateral = _position.collateral[collateralIndex] - assets;
        _position.collateral[collateralIndex] = UtilsLib.toUint128(newCollateral);
```

**File:** src/Midnight.sol (L631-634)
```text
            _marketState.lossFactor = UtilsLib.toUint128(
                type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
            );
            _marketState.totalUnits -= UtilsLib.toUint128(badDebt);
```

**File:** src/Midnight.sol (L805-807)
```text
        uint256 postSlashCredit = _lastLossFactor < type(uint128).max
            ? credit.mulDivDown(type(uint128).max - marketState[id].lossFactor, type(uint128).max - _lastLossFactor)
            : 0;
```

**File:** src/Midnight.sol (L832-851)
```text
    function _updatePosition(Market memory market, bytes32 id, address user)
        internal
        returns (uint128, uint128, uint128)
    {
        Position storage _position = position[id][user];
        (uint128 newCredit, uint128 newPendingFee, uint128 accruedFee) = updatePositionView(market, id, user);

        uint128 creditDecrease = _position.credit - newCredit;
        uint128 pendingFeeDecrease = _position.pendingFee - newPendingFee;

        _position.credit = newCredit;
        _position.lastLossFactor = marketState[id].lossFactor;
        _position.pendingFee = newPendingFee;
        _position.lastAccrual = uint128(block.timestamp);
        marketState[id].continuousFeeCredit += UtilsLib.toUint128(accruedFee);

        emit EventsLib.UpdatePosition(id, user, creditDecrease, pendingFeeDecrease, accruedFee);

        return (newCredit, newPendingFee, accruedFee);
    }
```
