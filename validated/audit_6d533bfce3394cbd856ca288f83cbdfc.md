Audit Report

## Title
Unchecked `takerCallback` as `payer` enables draining any approved `IBuyCallback` contract via sell-offer take - (File: src/Midnight.sol)

## Summary
In `take`, when a sell offer is taken (`offer.buy = false`), the attacker-supplied `takerCallback` is unconditionally assigned as `buyerCallback` and therefore as `payer`, with no authorization check linking it to `taker` or `msg.sender`. Any contract that has approved Midnight for `loanToken` and implements `IBuyCallback.onBuy` returning `CALLBACK_SUCCESS` without verifying it initiated the call can be fully drained of `buyerAssets` in a single transaction by an unprivileged attacker, who receives the corresponding protocol credit at the victim's expense.

## Finding Description
**Root cause:** The sole authorization check in `take` (line 346) validates `msg.sender` against `taker` but imposes no constraint on `takerCallback`:

```solidity
require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
```

**Exact code path (sell offer, `offer.buy = false`):**

At line 375, roles are assigned:
```solidity
(address buyer, address seller) = offer.buy ? (offer.maker, taker) : (taker, offer.maker);
// buyer = taker (attacker), seller = offer.maker
```

At lines 420–422, callback and payer are derived entirely from attacker-controlled input:
```solidity
address buyerCallback = offer.buy ? offer.callback : takerCallback;  // = victimAddress
address sellerCallback = offer.buy ? takerCallback : offer.callback;
address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
// payer = victimAddress
```

At lines 445–452, `IBuyCallback(victimAddress).onBuy(...)` is called. The `buyer` parameter passed is the attacker's `taker` address — not the callback contract itself — so the victim receives no obvious signal it was invoked externally. The `IBuyCallback` interface (src/interfaces/ICallbacks.sol, line 9) confirms `buyer` is a plain `address` parameter, not `msg.sender` or the callback contract:

```solidity
function onBuy(bytes32 id, Market memory market, uint256 buyerAssets, uint256 units, uint256 pendingFeeIncrease, address buyer, bytes memory data) external returns (bytes32);
```

If the victim returns `CALLBACK_SUCCESS` (e.g., only checking `msg.sender == midnight`, which passes since Midnight is the caller), execution continues to lines 455–456:

```solidity
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

The full `buyerAssets` is pulled from `payer = victimAddress`: `(buyerAssets - sellerAssets)` goes to Midnight as settlement fee, and `sellerAssets` goes to `offer.receiverIfMakerIsSeller`. The attacker (as `buyer = taker`) receives `units` of protocol credit — a real, withdrawable lending position — funded entirely by the victim.

**Why existing checks fail:** The `EcrecoverRatifier` validates only the maker's signed `Offer` struct; `takerCallback` is not a field of `Offer` and is never signed or validated. The authorization check at line 346 covers only `taker` vs `msg.sender`. There is no check of the form `require(takerCallback == address(0) || takerCallback == taker || isAuthorized[takerCallback][msg.sender])`.

## Impact Explanation
Direct, complete loss of `buyerAssets` worth of `loanToken` from the victim contract per exploit call. The attacker pays nothing and receives `units` of protocol credit (a real lending position redeemable at maturity) while the victim funds the entire trade. The attacker can repeat this for any amount up to the victim's balance and approval. This constitutes unauthorized theft of assets from a third-party contract — a critical, in-scope impact.

## Likelihood Explanation
**Preconditions:**
1. A valid sell offer exists (normal protocol usage).
2. A victim contract exists with `loanToken.approve(midnight, ...)` and `onBuy` returning `CALLBACK_SUCCESS` without checking `buyer == address(this)` — satisfied by any Midnight-integrated buyer callback contract that only guards against `msg.sender != midnight` (which passes, since Midnight is the caller).
3. Attacker is any unprivileged taker — no special role required.

The attack requires no flash loan, no oracle manipulation, no governance action, and is executable in a single transaction. It is fully repeatable up to the victim's balance and approval. The protocol provides no documentation warning that `onBuy` implementations must verify `buyer == address(this)`, making unguarded implementations realistic.

## Recommendation
Add an authorization check on `takerCallback` before it is used as `payer`. For example, in `take`, after the existing `taker` authorization check:

```solidity
require(
    takerCallback == address(0) || takerCallback == taker || isAuthorized[takerCallback][msg.sender],
    TakerCallbackUnauthorized()
);
```

This ensures the callback/payer address is either absent, the taker themselves, or explicitly authorized by the taker. Alternatively, decouple the `payer` role from `buyerCallback` entirely: always use `taker` or `msg.sender` as payer and only invoke the callback for notification purposes, requiring the callback to pull funds itself.

## Proof of Concept
**Minimal Foundry test plan:**

1. Deploy `Midnight` and a mock ERC-20 `loanToken`.
2. Deploy `VictimBuyerCallback` that:
   - Holds `loanToken` balance and has `approve(midnight, type(uint256).max)`.
   - Implements `onBuy(...)` returning `CALLBACK_SUCCESS` without checking `buyer == address(this)`.
3. Create a sell offer (`offer.buy = false`) from `maker` with a valid ratifier.
4. As attacker (`msg.sender = taker`), call:
   ```solidity
   midnight.take(offer, ratifierData, units, taker, receiver, address(victimBuyerCallback), "");
   ```
5. Assert:
   - `victimBuyerCallback`'s `loanToken` balance decreased by `buyerAssets`.
   - `position[id][taker].credit` increased by `units`.
   - Attacker paid zero `loanToken`.