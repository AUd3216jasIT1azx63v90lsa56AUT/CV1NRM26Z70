Audit Report

## Title
Zero-asset take bypasses full-consumption guard on buy offers with `buyerPrice < WAD` - (File: `src/Midnight.sol`)

## Summary
When `offer.buy == true` and `offer.maxAssets > 0`, the consumed accounting increments by `buyerAssets = units.mulDivDown(buyerPrice, WAD)`, which truncates to zero when `units * buyerPrice < WAD`. Because the cap check uses `<=` rather than `<`, a taker can call `take` with `units > 0` after the offer is fully consumed (`consumed == maxAssets`), pass the guard with `newConsumed = maxAssets + 0 = maxAssets`, and mutate position state (credit, debt, `totalUnits`) without transferring any assets. The test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` explicitly names and demonstrates this as a bug.

## Finding Description

**Root cause — `src/Midnight.sol` lines 363 and 367–369:**

`buyerAssets` is computed with `mulDivDown`, which performs integer division and truncates to zero whenever `units * buyerPrice < WAD`: [1](#0-0) 

The consumed cap check then adds this (potentially zero) value and enforces `<=`: [2](#0-1) 

When `consumed == maxAssets` and `buyerAssets == 0`, `newConsumed = maxAssets + 0 = maxAssets ≤ maxAssets` — the check passes unconditionally.

**Exploit flow:**
1. Taker fills the offer in two legitimate steps until `consumed[maker][group] == maxAssets`.
2. Taker calls `take(offer, ..., units=1)` where `buyerPrice < WAD` (any tick where `offerPrice < WAD`). `mulDivDown(1 * buyerPrice, WAD) = 0`. The cap check passes.
3. Position state mutates: `buyerPos.credit`, `sellerPos.debt`, and `_marketState.totalUnits` all increase by `units`, but zero tokens are transferred. [3](#0-2) 

Step 3 is repeatable indefinitely on the same fully-consumed offer.

**Why existing checks fail:** There is no floor check requiring `buyerAssets > 0` when `units > 0`. The `<=` guard only prevents `newConsumed` from exceeding `maxAssets`; it does not prevent a zero-increment take from proceeding once the cap is already reached.

## Impact Explanation

A fully-consumed buy offer can be taken an unbounded number of additional times with `units > 0` and zero asset transfer. Each such take manufactures unbacked credit for the maker (buyer) and unbacked debt for the taker (seller), and inflates `_marketState.totalUnits`. This directly violates the core accounting invariant that `consumed == maxAssets` means no further fills are permitted, enabling creation of unbacked credit/debt — a critical integrity failure in the protocol's financial state.

## Likelihood Explanation

All preconditions are reachable by an unprivileged taker:
- `offer.buy == true`, `offer.maxAssets > 0` — standard offer configuration.
- `buyerPrice < WAD` — any tick where `offerPrice < WAD` (i.e., a negative tick); fully attacker-controlled via `offer.tick`.
- `consumed == maxAssets` — reachable by the attacker themselves via prior legitimate fills.

No privileged access, leaked keys, or victim mistakes are required. The attack is repeatable indefinitely on the same offer after full consumption.

## Recommendation

Add an explicit guard requiring that `buyerAssets > 0` whenever `units > 0` and `offer.maxAssets > 0` with `offer.buy == true`. For example, immediately after computing `buyerAssets`:

```solidity
if (offer.maxAssets > 0 && offer.buy) {
    require(buyerAssets > 0, ZeroAssetTake());
}
```

Alternatively, enforce a minimum of 1 for `buyerAssets` when `units > 0` (i.e., use a floor of 1 in the rounding), or change the consumed check to `< offer.maxAssets` and separately handle the final partial fill. The root fix must ensure that a non-zero `units` take always results in a non-zero asset transfer when `maxAssets` accounting is active.

## Proof of Concept

The test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` explicitly demonstrates this bug. [4](#0-3) 

Manual reproduction steps:
1. Create a buy offer with `maxAssets = M` and `tick` such that `offerPrice < WAD`.
2. Fill the offer in two calls until `consumed[maker][group] == M`.
3. Call `take(offer, ..., units=1)` — observe that the call succeeds, `consumed` remains `M`, but `buyerPos.credit`, `sellerPos.debt`, and `totalUnits` each increase by 1 with zero token transfer.
4. Repeat step 3 arbitrarily many times to accumulate unbacked credit/debt.

### Citations

**File:** src/Midnight.sol (L363-364)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/Midnight.sol (L367-369)
```text
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L408-417)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);

        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
```

**File:** test/TakeTest.sol (L1-1)
```text
// SPDX-License-Identifier: GPL-2.0-or-later
```
