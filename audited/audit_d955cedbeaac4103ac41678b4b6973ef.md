Audit Report

## Title
Assets-mode consumed counter not incremented when `buyerAssets` rounds to zero, bypassing `maxAssets` cap - (File: src/Midnight.sol)

## Summary
When a buy offer has `maxAssets > 0` and `buyerPrice < WAD`, calling `take()` with `units=1` computes `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`. The consumed counter is incremented by zero, so the `ConsumedAssets` guard passes trivially on every call. Position credit and debt are updated unconditionally on `units`, allowing unbounded credit accumulation for the maker far beyond the `maxAssets` cap they intended.

## Finding Description
**Root cause — line 363 and 368, `src/Midnight.sol`:**

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
// ...
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

When `offer.buy = true`, `units = 1`, and `buyerPrice < WAD` (any tick below 2910), `mulDivDown(1, buyerPrice, WAD)` floors to zero. The consumed mapping is incremented by zero, so `newConsumed` equals its pre-call value. The guard `require(newConsumed <= offer.maxAssets)` is trivially satisfied even on a fully-consumed offer.

**Position accounting — lines 382–414, `src/Midnight.sol`:**

```solidity
uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
uint256 sellerDebtIncrease = units - sellerCreditDecrease;
// ...
buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);
sellerPos.debt   += UtilsLib.toUint128(sellerDebtIncrease);
```

These lines operate on `units`, not `buyerAssets`. With `units = 1`, the maker (buyer) gains 1 unit of credit and the taker (seller) gains 1 unit of debt per call, regardless of whether `buyerAssets` rounded to zero. No token transfer occurs because `buyerAssets - sellerAssets = 0`.

**Protocol acknowledgment — line 94, `src/Midnight.sol`:**

```
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

The protocol explicitly documents this gap. The test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` directly confirms the state change: `consumed` stays frozen at `maxAssets`, token balances are unchanged, yet `creditOf(maker)` and `debtOf(taker)` both strictly increase after each `take(units=1)` on a fully-consumed offer.

**Why existing checks fail:** The `ConsumedAssets` guard only checks `newConsumed <= offer.maxAssets`. When the increment is zero, `newConsumed` never advances past the pre-call value, so the guard is permanently satisfied regardless of how many times `take` is called.

## Impact Explanation
The maker's credit grows without bound: each `take(units=1)` call adds 1 unit of credit while `consumed` stays frozen. The maker's intended cap of `maxAssets / buyerPrice` units of credit exposure is never enforced. Because bad debt is socialized proportionally to credit held at realization time, the maker suffers losses proportional to their inflated credit balance rather than their intended exposure. This constitutes a concrete accounting insolvency risk for the maker — their actual loss at bad-debt realization can be an unbounded multiple of what `maxAssets` was meant to cap.

## Likelihood Explanation
The precondition `buyerPrice < WAD` is satisfied for any tick below 2910, which is the natural range for buy offers (the lower half of `[0, MAX_TICK=5820]`). Once an offer is first taken, the ratifier signature is public on-chain, so any unprivileged address can replay `take(units=1)` with no per-iteration token cost beyond gas. The attack is fully permissionless and indefinitely repeatable. The taker accumulates debt without receiving assets, but a griefing attacker using a throwaway account (or one that already holds credit in the market to offset the debt) faces no meaningful barrier.

## Recommendation
In the `offer.maxAssets > 0` branch, enforce that `buyerAssets > 0` whenever `units > 0` for buy offers:

```solidity
if (offer.maxAssets > 0) {
    uint256 consumedDelta = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || consumedDelta > 0, ZeroBuyerAssets());
    newConsumed = consumed[offer.maker][offer.group] += consumedDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, fall back to incrementing by `units` when `buyerAssets` rounds to zero, so the consumed counter always advances when position state changes. Either approach closes the gap between the consumed-counter update and the position-accounting update.

## Proof of Concept
1. Deploy a market with `buyerPrice < WAD` (any tick < 2910, e.g. tick = 0 → price ≈ 0).
2. Create a buy offer with `maxAssets = N` (e.g. `N = 100`) and a valid ratifier signature.
3. Call `take(units=1, ...)` once to consume the offer up to `maxAssets` via normal means (or set `consumed` directly to `N` via `setConsumed`).
4. Call `take(units=1, ...)` again. Observe: `consumed[maker][group]` remains `N`, `creditOf(maker)` increases by 1, `debtOf(taker)` increases by 1, token balances unchanged.
5. Repeat step 4 arbitrarily many times. `creditOf(maker)` grows without bound while `consumed` stays frozen at `N`.

The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` reproduces exactly this sequence.