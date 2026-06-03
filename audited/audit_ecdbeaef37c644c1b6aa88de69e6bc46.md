Audit Report

## Title
Unprivileged Attacker Can Drain Any Approved `IBuyCallback` Contract via Unvalidated `takerCallback` Payer Assignment - (File: src/Midnight.sol)

## Summary
In `Midnight.take`, when processing a sell offer (`offer.buy = false`), the attacker-supplied `takerCallback` is assigned as `buyerCallback` and consequently as `payer` with no check that it is authorized by the taker. Any contract that has approved Midnight for the loan token and implements `IBuyCallback` returning `CALLBACK_SUCCESS` on any call from Midnight can be drained by an unprivileged attacker who designates it as `takerCallback`.

## Finding Description

**Root cause — `src/Midnight.sol` lines 420–456:**

When `offer.buy = false` (sell offer), the taker is the buyer. The `takerCallback` parameter is fully attacker-controlled and is assigned without any authorization check:

```solidity
address buyerCallback = offer.buy ? offer.callback : takerCallback; // line 420: attacker-supplied
address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender); // line 422: payer = victim
```

The only authorization check in `take` is at line 346:

```solidity
require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
```

This validates the caller's right to act as `taker`, but says nothing about `takerCallback`. There is no check that `takerCallback == taker`, `takerCallback == msg.sender`, or `isAuthorized[taker][takerCallback]`.

After the callback returns `CALLBACK_SUCCESS`, tokens are pulled from `payer` (the victim):

```solidity
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets); // line 455
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets); // line 456
```

**Exploit flow:**
1. Victim contract `V` has `loanToken.approve(midnight, type(uint256).max)` and implements `IBuyCallback`, returning `CALLBACK_SUCCESS` from `onBuy` for any call where `msg.sender == midnight` (the natural trust boundary for a Midnight integrator).
2. Attacker calls `midnight.take(sellOffer, ratifierData, units, attacker, receiver, address(V), callbackData)`.
3. `buyerCallback = address(V)`, `payer = address(V)`.
4. Midnight calls `V.onBuy(id, market, buyerAssets, units, pendingFeeIncrease, taker, callbackData)` → `V` returns `CALLBACK_SUCCESS` (it only checks `msg.sender == midnight`, not `buyer == address(this)`).
5. Midnight executes `safeTransferFrom(loanToken, V, address(this), buyerAssets - sellerAssets)` and `safeTransferFrom(loanToken, V, receiver, sellerAssets)`.
6. `V` loses `buyerAssets` in loan tokens; attacker's position gains credit for `units` at zero personal cost.

**Why existing checks fail:**

The `onBuy` interface (`src/interfaces/ICallbacks.sol` line 9) passes `buyer` as a parameter, but the protocol does not require — and does not document — that the callback contract must verify `buyer == address(this)`. A contract that only guards `msg.sender == midnight` is a correct and natural Midnight integrator implementation; the protocol is the expected trust anchor. The protocol itself is the only place where `takerCallback` authorization can be enforced, and it does not enforce it. The Certora spec (`certora/specs/OnlyExplicitPayerCanLoseTokens.spec` lines 91–115) explicitly models the buyerCallback's `CALLBACK_SUCCESS` return as the consent mechanism, but this is insufficient because the callback cannot distinguish a legitimate invocation from an attacker-injected one without additional protocol-level guarantees.

## Impact Explanation
Direct theft of loan tokens from any contract that (a) has approved Midnight for the loan token and (b) implements `IBuyCallback` returning `CALLBACK_SUCCESS` without verifying it was legitimately designated as the callback. The attacker acquires protocol credit (`buyerCreditIncrease` units) for free. The victim's token balance decreases by `buyerAssets = units.mulDivUp(buyerPrice, WAD)`, unbounded up to the victim's balance and approval. The loss is immediate and unrecoverable.

## Likelihood Explanation
All three preconditions are realistic in a live ecosystem: (1) any router, aggregator, or vault interacting with Midnight will have approved the loan token; (2) any legitimate Midnight buy-callback integrator will return `CALLBACK_SUCCESS` from `onBuy` when called by Midnight, as the protocol is the expected trust anchor; (3) valid sell offers exist in any active market. The attack requires no special privilege, is repeatable up to the victim's balance and approval, and costs the attacker only gas.

## Recommendation
Add an authorization check on `takerCallback` before it is used as `payer`. For example:

```solidity
require(
    takerCallback == address(0) || takerCallback == taker || takerCallback == msg.sender
        || isAuthorized[taker][takerCallback],
    UnauthorizedTakerCallback()
);
```

This ensures the taker explicitly consents to the callback contract acting as payer on their behalf, consistent with the existing `isAuthorized` authorization model used throughout the protocol.

## Proof of Concept
1. Deploy a victim contract `V` that implements `IBuyCallback.onBuy` returning `CALLBACK_SUCCESS` when `msg.sender == midnight` (no check on `buyer`).
2. Fund `V` with `loanToken` and call `loanToken.approve(midnight, type(uint256).max)` from `V`.
3. Create a valid sell offer from a maker with sufficient credit.
4. As attacker, call `midnight.take(sellOffer, ratifierData, units, attacker, attacker, address(V), "")`.
5. Assert: `V`'s `loanToken` balance decreased by `buyerAssets`; attacker's credit position increased by `buyerCreditIncrease`; attacker paid zero tokens.