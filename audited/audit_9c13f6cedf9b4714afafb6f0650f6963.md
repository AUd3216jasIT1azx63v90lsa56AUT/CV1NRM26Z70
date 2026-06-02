Audit Report

## Title
Reentrancy in `take()` buyer callback allows draining pre-existing `withdrawable` balance at a discount - (`src/Midnight.sol`)

## Summary
In `take()`, the buyer's credit is written to storage and `onBuy` is called on the maker-controlled callback before the `safeTransferFrom` that settles payment. Because `withdraw()` has no reentrancy guard and no transient lock protecting it during `take()` callbacks, a malicious `IBuyCallback` authorized by the maker can call `withdraw()` inside `onBuy`, consuming pre-existing `withdrawable` balance (funded by other borrowers' repayments) and using those tokens to fund the take's own payment. The attacker nets `units - buyerAssets` tokens at the expense of other lenders' withdrawable pool.

## Finding Description

**Root cause:** `take()` violates checks-effects-interactions: it writes `buyerPos.credit` to storage at line 410, then makes an external call to `IBuyCallback.onBuy` at lines 445–453, and only then pulls tokens via `safeTransferFrom` at lines 455–456. The `LIQUIDATION_LOCK_SLOT` set at line 444 only prevents liquidation of the seller; it does not protect `withdraw()`. `withdraw()` (lines 481–500) has no reentrancy guard and no check that it is being called during a `take()` callback.

**Exact code path:**

`take()` with `offer.buy == true`:
- Line 410: `buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease)` — credit written to storage before any external call.
- Line 420: `buyerCallback = offer.callback` — fully maker-controlled.
- Line 422: `payer = buyerCallback` — the callback is also the payer for the subsequent `safeTransferFrom`.
- Lines 445–453: `IBuyCallback(buyerCallback).onBuy(...)` — external call before token transfer.
- Lines 455–456: `safeTransferFrom(loanToken, payer, address(this), buyerAssets - sellerAssets)` and `safeTransferFrom(loanToken, payer, receiver, sellerAssets)` — token pull happens after callback returns.

`withdraw()` (lines 481–500):
- Line 482: `isAuthorized[onBehalf][msg.sender]` — satisfied if maker pre-authorized the callback.
- Line 493: `_position.credit -= units` — succeeds because credit was just increased at line 410.
- Line 494: `_marketState.withdrawable -= units` — succeeds if pre-existing `withdrawable ≥ units`.
- Line 499: transfers `units` tokens to receiver.

**Exploit flow:**

Precondition: market has pre-existing `withdrawable = W ≥ U` from prior borrower repayments (via `repay()` line 509 or `liquidate()` line 675).

1. Maker deploys `MaliciousCallback` implementing `IBuyCallback`; calls `setIsAuthorized(MaliciousCallback, true, maker)` so `isAuthorized[maker][MaliciousCallback] = true`.
2. Maker creates buy offer with `offer.callback = MaliciousCallback`, ratified via a maker-controlled ratifier.
3. Taker calls `take(offer, ratifierData, U, taker, receiver, address(0), hex"")`.
4. `take()` sets `buyerPos.credit += U` (line 410).
5. `take()` calls `MaliciousCallback.onBuy(...)` (line 448).
6. Inside `onBuy`, `MaliciousCallback` calls `Midnight.withdraw(market, U, maker, attacker)`:
   - Auth: `isAuthorized[maker][MaliciousCallback]` = true, `msg.sender` = MaliciousCallback ✓
   - Credit: `_position.credit = U ≥ U` ✓
   - Withdrawable: `_marketState.withdrawable = W ≥ U` ✓
   - Result: `_position.credit = 0`, `withdrawable = W - U`, attacker receives U tokens.
7. `MaliciousCallback` approves Midnight for `buyerAssets` tokens (funded by the U tokens just received; since `buyerPrice < WAD`, `buyerAssets < U`).
8. `onBuy` returns `CALLBACK_SUCCESS`.
9. `take()` calls `safeTransferFrom(loanToken, MaliciousCallback, address(this), buyerAssets - sellerAssets)` and `safeTransferFrom(loanToken, MaliciousCallback, receiver, sellerAssets)` — both succeed.

**Why existing checks fail:**
- `LIQUIDATION_LOCK_SLOT` (line 444) only prevents liquidation of the seller; it does not block `withdraw()`.
- `withdraw()` has no transient lock, no reentrancy guard, and no check that it is not being called during a `take()` callback.
- There is no post-callback invariant check in `take()` that `withdrawable` was not consumed.
- The `_updatePosition` call inside `withdraw()` (line 485) is a no-op in the same block since `_updatePosition` was already called for the buyer at line 379 of `take()`.

## Impact Explanation

Direct theft of tokens from the `withdrawable` pool. The attacker receives `U` tokens immediately from the pre-existing `withdrawable` pool while paying only `buyerAssets = U × buyerPrice / WAD < U` tokens. Net profit per attack: `U × (1 - buyerPrice/WAD)`. Other lenders who had repaid tokens available for withdrawal find `withdrawable` reduced by `U`, and the contract's token balance is short by `U - (buyerAssets - sellerAssets)` with no accounting correction. This is a direct, concrete theft of other lenders' withdrawable assets.

## Likelihood Explanation

Preconditions are realistic in any active market: (a) pre-existing `withdrawable` balance exists whenever any borrower has repaid; (b) the maker can freely set `offer.callback` and authorize it via `setIsAuthorized` — no privileged role required; (c) the ratifier only requires that the maker (or their authorized operator) ratify the offer root. The attack is repeatable as long as `withdrawable` is non-zero and is profitable whenever `buyerPrice < WAD` (the normal lending case). The taker does not need to be malicious or even aware of the attack.

## Recommendation

Apply one or more of the following:

1. **Transient reentrancy lock on `withdraw()`:** Introduce a transient storage lock (analogous to `LIQUIDATION_LOCK_SLOT`) that is set during `take()` callbacks and checked at the top of `withdraw()`, reverting if set.
2. **Post-callback `withdrawable` invariant:** After the buyer callback returns in `take()`, assert that `_marketState.withdrawable` has not decreased relative to its value before the callback.
3. **Checks-effects-interactions in `take()`:** Move the `safeTransferFrom` calls before the buyer callback. This requires the payer to hold tokens before `onBuy` is called, which changes the callback semantics but eliminates the window entirely.

Option 1 is the least invasive and consistent with the existing `LIQUIDATION_LOCK_SLOT` pattern already used in `take()`.

## Proof of Concept

Minimal Foundry test outline:

```solidity
// Setup: market with withdrawable = W (from a prior repay())
// Attacker: maker with MaliciousCallback authorized

contract MaliciousCallback is IBuyCallback {
    Midnight midnight;
    Market market;
    address maker;
    address attacker;

    function onBuy(bytes32, Market memory, uint256 buyerAssets, uint256 units, ...)
        external returns (bytes4)
    {
        // At this point, maker.credit == units (just set by take())
        // and withdrawable == W >= units
        midnight.withdraw(market, units, maker, attacker);
        // attacker now holds `units` tokens
        // approve Midnight to pull buyerAssets (< units) for the take settlement
        IERC20(market.loanToken).approve(address(midnight), buyerAssets);
        return CALLBACK_SUCCESS;
    }
}

function testReentrancyDrainsWithdrawable() public {
    // 1. Borrower repays, setting withdrawable = W
    // 2. Maker deploys MaliciousCallback, authorizes it
    // 3. Maker creates buy offer with callback = MaliciousCallback, price < WAD
    // 4. Taker calls take() with units = W
    // Assert: attacker.balance increased by W
    // Assert: withdrawable == 0
    // Assert: contract token balance decreased by W - buyerAssets
}
```

Expected result: attacker receives `W` tokens, pays back only `buyerAssets < W`, netting `W - buyerAssets` tokens from the `withdrawable` pool.