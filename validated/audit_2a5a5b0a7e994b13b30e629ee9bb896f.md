Audit Report

## Title
Unchecked `takerCallback` as `payer` enables draining any approved `IBuyCallback` contract via sell-offer take - (File: src/Midnight.sol)

## Summary
In `take`, when a sell offer is taken (`offer.buy = false`), the attacker-supplied `takerCallback` is assigned as both `buyerCallback` and `payer` with no authorization check tying it to `taker` or `msg.sender`. Any contract that has approved Midnight for `loanToken` and implements `IBuyCallback` returning `CALLBACK_SUCCESS` without an initiation guard can be drained of `buyerAssets` in a single `take` call by an unprivileged attacker, who receives the corresponding protocol credit at the victim's expense.

## Finding Description

**Root cause:** The sole authorization check in `take` is:

```solidity
// Line 346
require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
```

This validates `msg.sender` against `taker` but imposes no constraint on `takerCallback`. There is no check of the form `require(takerCallback == address(0) || takerCallback == taker || isAuthorized[takerCallback][msg.sender])`.

**Exact code path (sell offer, `offer.buy = false`):**

At line 375, roles are assigned:
```solidity
(address buyer, address seller) = offer.buy ? (offer.maker, taker) : (taker, offer.maker);
// buyer = taker (attacker), seller = offer.maker
```

At lines 420–422, callback and payer are derived entirely from attacker input:
```solidity
address buyerCallback = offer.buy ? offer.callback : takerCallback;  // = victimAddress
address sellerCallback = offer.buy ? takerCallback : offer.callback; // = offer.callback
address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
// payer = victimAddress
```

At lines 445–452, `IBuyCallback(victimAddress).onBuy(...)` is called. The `buyer` parameter passed is the attacker's `taker` address — not the callback contract itself — so the victim has no obvious signal it was invoked externally. If the victim returns `CALLBACK_SUCCESS` without an initiation guard, execution continues.

At lines 455–456, tokens are pulled from `payer = victimAddress`:
```solidity
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

The full `buyerAssets` is drained from the victim: `(buyerAssets - sellerAssets)` (the settlement fee) goes to Midnight, and `sellerAssets` goes to `offer.receiverIfMakerIsSeller` (the maker's pre-set receiver). The attacker (as `buyer`) receives `units` of protocol credit — a real, withdrawable lending position — funded entirely by the victim.

**Why `EcrecoverRatifier` does not stop it:** The ratifier validates only the maker's signed `Offer` struct. `takerCallback` is not a field of `Offer` and is never signed or validated.

**Why victim `onBuy` returning `CALLBACK_SUCCESS` is realistic:** Any smart-contract buyer integrating with Midnight will (a) hold an approval to Midnight for `loanToken`, and (b) implement `onBuy` returning `CALLBACK_SUCCESS`. The protocol provides no documentation warning that `onBuy` implementations must verify they initiated the call. The `onBuy` signature passes `buyer` (the attacker's `taker` address), not the callback contract itself, so the victim has no obvious signal it was invoked externally.

## Impact Explanation
Direct, complete loss of `buyerAssets` worth of `loanToken` from the victim contract per exploit call. The attacker pays nothing — they receive `units` of protocol credit (a real lending position redeemable at maturity) while the victim funds the entire trade. The attacker can repeat this for any amount up to the victim's balance and approval. This constitutes unauthorized theft of assets from a third-party contract.

## Likelihood Explanation
**Preconditions:**
1. A valid sell offer exists (normal protocol usage).
2. A victim contract exists with `loanToken.approve(midnight, ...)` and `onBuy` returning `CALLBACK_SUCCESS` without an initiation guard — satisfied by any Midnight-integrated buyer callback contract that does not implement a reentrancy/initiation guard.
3. Attacker is any unprivileged taker — no special role required.

The attack requires no flash loan, no oracle manipulation, no governance action, and is executable in a single transaction. It is fully repeatable up to the victim's balance and approval.

## Recommendation
Add an authorization check on `takerCallback` before it is used as `payer`. For example:

```solidity
require(
    takerCallback == address(0) || takerCallback == taker || isAuthorized[taker][takerCallback],
    TakerCallbackUnauthorized()
);
```

This mirrors the existing `isAuthorized` pattern used throughout the protocol and ensures the payer is always a party that `taker` has explicitly authorized to act on their behalf. Alternatively, document clearly (and enforce via interface) that `onBuy` implementations must verify `msg.sender == address(midnight)` AND that they initiated the call via an internal flag, and add a NatSpec warning to `take`.

## Proof of Concept

**Minimal test plan (Foundry):**

1. Deploy a mock `VictimBuyer` contract that:
   - Implements `IBuyCallback.onBuy` returning `CALLBACK_SUCCESS` unconditionally (no initiation guard).
   - Has `loanToken.approve(address(midnight), type(uint256).max)` and holds `buyerAssets` of `loanToken`.

2. Create a valid sell offer signed by a maker via `EcrecoverRatifier`.

3. As an unprivileged attacker (`taker = attacker`), call:
   ```solidity
   midnight.take(offer, ratifierData, units, attacker, address(0), address(victimBuyer), "");
   ```

4. Assert:
   - `loanToken.balanceOf(address(victimBuyer))` decreased by `buyerAssets`.
   - `midnight.creditOf(id, attacker)` increased by `units`.
   - Attacker paid zero `loanToken`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** src/Midnight.sol (L346-346)
```text
        require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
```

**File:** src/Midnight.sol (L420-422)
```text
        address buyerCallback = offer.buy ? offer.callback : takerCallback;
        address sellerCallback = offer.buy ? takerCallback : offer.callback;
        address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
```

**File:** src/Midnight.sol (L445-456)
```text
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

**File:** src/interfaces/ICallbacks.sol (L8-10)
```text
interface IBuyCallback {
    function onBuy(bytes32 id, Market memory market, uint256 buyerAssets, uint256 units, uint256 pendingFeeIncrease, address buyer, bytes memory data) external returns (bytes32);
}
```
