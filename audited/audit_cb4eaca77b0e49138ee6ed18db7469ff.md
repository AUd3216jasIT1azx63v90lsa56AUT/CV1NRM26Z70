Audit Report

## Title
`onBuy` Callback Reentrancy Drains `withdrawable` Pool Before Asset Transfer - (File: `src/Midnight.sol`)

## Summary
In `take`, buyer credit is committed to storage at line 410 and `claimableSettlementFee` is updated at line 418 before the external `onBuy` callback fires at line 448, while the payer's token pull does not occur until lines 455–456 after the callback returns. An attacker who controls the `buyerCallback` contract can reenter `withdraw` during `onBuy`, redeeming the freshly minted credit against the pre-existing `withdrawable` pool to receive `units` tokens, then using those tokens to satisfy the payer transfer of only `buyerAssets < units`. The net result is extraction of `units − buyerAssets` tokens from other lenders' withdrawable balance in a single atomic transaction.

## Finding Description

**Root cause:** `take` writes all buyer-side state mutations (`buyerPos.credit`, `totalUnits`, `claimableSettlementFee`) before making the external `onBuy` call, but defers the token pull until after the callback returns. `withdraw` has no reentrancy guard and no check that it is not being called mid-`take`.

**Exact code path:**

`take` (sell offer, `offer.buy = false`):
- Line 375: `buyer = taker` (attacker), `seller = offer.maker`
- Line 420–422: `buyerCallback = takerCallback` (attacker-controlled contract C); `payer = buyerCallback = C`
- Line 410: `buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease)` — credit committed to storage
- Line 418: `claimableSettlementFee[...] += buyerAssets - sellerAssets` — fee committed
- Line 444: `LIQUIDATION_LOCK_SLOT` set only for the *seller*; no lock on `withdraw`
- Lines 448–452: `IBuyCallback(buyerCallback).onBuy(...)` — external call with no reentrancy protection
- Lines 455–456: `safeTransferFrom(payer, ...)` — asset pull happens **after** callback returns

`withdraw` (called inside `onBuy` by C on behalf of attacker):
- Line 482: `isAuthorized[attacker][C]` — satisfied via prior `setIsAuthorized(C, true, attacker)`
- Line 485: `_updatePosition` — no-op in same block
- Line 493: `_position.credit -= units` — succeeds; credit was just set at line 410
- Line 494: `_marketState.withdrawable -= units` — succeeds if `withdrawable >= units`; no guard prevents this during a `take` callback
- Line 495: `_marketState.totalUnits -= units` — reverts the `totalUnits` increase from `take`
- Line 499: `safeTransfer(loanToken, C, units)` — `units` tokens leave the contract

**Exploit flow:**
1. Attacker calls `setIsAuthorized(C, true, attacker)`.
2. Market has `withdrawable = W ≥ units` (from any prior repayment or liquidation).
3. Attacker calls `take(sellOffer, ..., attacker, ..., C, ...)` with `units` chosen so `buyerAssets = units × buyerPrice / WAD < units` (any tick with `buyerPrice < WAD`).
4. `take` writes `buyerPos.credit += units` (line 410), then calls `C.onBuy(...)` (line 448).
5. Inside `C.onBuy`: C calls `midnight.withdraw(market, units, attacker, C)`.
   - `attacker.credit -= units` → 0
   - `withdrawable -= units` → `W − units`
   - `totalUnits` reverts to pre-take value
   - Contract sends `units` tokens to C
6. `C.onBuy` returns `CALLBACK_SUCCESS`.
7. `take` resumes: `safeTransferFrom(C, address(this), buyerAssets − sellerAssets)` and `safeTransferFrom(C, receiver, sellerAssets)` — C pays only `buyerAssets` total using the tokens it just received.
8. C retains `units − buyerAssets` tokens as profit.

**Why existing checks fail:**
- `LIQUIDATION_LOCK_SLOT` (line 444) only blocks liquidation of the seller; it does not block `withdraw`.
- There is no reentrancy guard on `withdraw` or `take`.
- The TOKEN SAFETY REQUIREMENTS (lines 133–140) prohibit re-entry from token transfers, not from `onBuy` callbacks — this attack path re-enters through the callback, not the token.

## Impact Explanation

The attacker extracts `units − buyerAssets = units × (WAD − buyerPrice) / WAD` tokens per call from the `withdrawable` pool. These tokens represent repaid loan assets owed to other lenders. After the attack, `withdrawable` is permanently reduced by `units` while the attacker's credit is zero and no new debt is outstanding. Other lenders holding credit cannot redeem their proportional share until new repayments refill the pool. The entire `withdrawable` pool can be drained in a single transaction by setting `units = withdrawable`. This constitutes direct, irreversible theft of lender funds — a critical impact matching the "direct loss of user funds" and "protocol insolvency" categories in `live_context.json`.

## Likelihood Explanation

**Preconditions:**
1. `withdrawable > 0` — satisfied any time a borrower has repaid or been liquidated (normal market operation).
2. A sell offer exists at any tick with `buyerPrice < WAD` — standard discount lending, always present in active markets.
3. Attacker pre-authorizes their callback contract via one `setIsAuthorized` call — no privilege required.

The attack is fully permissionless, requires no oracle manipulation, no admin access, and no special token behavior. It is executable in a single transaction and is repeatable across any market with repayment history.

## Recommendation

Add a transient reentrancy lock in `take` that is checked at the entry of `withdraw`. The existing `LIQUIDATION_LOCK_SLOT` pattern using `UtilsLib.tExchange` / `UtilsLib.tGet` demonstrates the correct approach with transient storage. A dedicated `TAKE_LOCK_SLOT` should be set to `true` at the start of `take` (before any state mutations) and cleared at the end, with `withdraw` requiring this slot to be `false`. Alternatively, move all buyer-side state mutations (lines 408–418) to after the token pull (lines 455–456), so no exploitable credit exists in storage when the callback fires. The latter is the more principled fix as it eliminates the window entirely rather than relying on a guard.

## Proof of Concept

**Minimal Foundry test plan:**

```solidity
// 1. Deploy attacker contract C implementing IBuyCallback
// 2. attacker calls midnight.setIsAuthorized(address(C), true, attacker)
// 3. Ensure market has withdrawable > 0 (e.g., via a prior repay call)
// 4. attacker calls midnight.take(sellOffer, ..., attacker, ..., address(C), ...)
//    where sellOffer.tick corresponds to buyerPrice < WAD
// 5. C.onBuy implementation:
//    function onBuy(..., uint256 units, ...) external returns (bytes4) {
//        midnight.withdraw(market, units, attacker, address(this));
//        // now this contract holds `units` tokens
//        // approve midnight to pull buyerAssets from this contract
//        loanToken.approve(address(midnight), buyerAssets);
//        return CALLBACK_SUCCESS;
//    }
// 6. Assert: C.balance == units - buyerAssets (profit)
// 7. Assert: midnight.withdrawable(id) == W - units (drained)
// 8. Assert: attacker credit == 0, no new debt outstanding
```

**Invariant to fuzz:** `contract_loanToken_balance >= withdrawable + claimableSettlementFee` — this invariant is broken after the attack because `withdrawable` is reduced by `units` but the contract only receives back `buyerAssets < units`. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** src/Midnight.sol (L408-418)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);

        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

**File:** src/Midnight.sol (L444-456)
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
```

**File:** src/Midnight.sol (L481-499)
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
```
