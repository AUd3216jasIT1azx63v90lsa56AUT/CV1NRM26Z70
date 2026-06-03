Audit Report

## Title
Buy-offer `maxAssets` cap bypassed via rounding-to-zero `buyerAssets` on small-unit takes - (File: src/Midnight.sol)

## Summary
When a buy offer has `maxAssets > 0` and `buyerPrice < WAD`, calling `take()` with sufficiently small `units` causes `buyerAssets = units.mulDivDown(buyerPrice, WAD)` to round to zero. The `consumed` counter is not incremented, so the `newConsumed <= offer.maxAssets` guard passes unconditionally — even after the offer is fully consumed — while `creditOf`, `debtOf`, and `totalUnits` all strictly increase. This allows unbounded unit-level overfill of the maker's offer beyond the asset-denominated cap.

## Finding Description
**Code path — `src/Midnight.sol` lines 363–369:**

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // rounds DOWN to zero
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

**Root cause:** For a buy offer, `consumed` is incremented by `buyerAssets`. When `buyerPrice < WAD` and `units * buyerPrice < WAD`, `mulDivDown` returns 0. `consumed` is unchanged, so `newConsumed <= offer.maxAssets` is trivially satisfied regardless of how many times `take()` is called — including after the offer has already been fully consumed via normal fills.

**Exploit flow:**
1. Maker creates a buy offer with `maxAssets = N` and `tick > MAX_TICK/2` (so `buyerPrice < WAD`).
2. Offer is fully consumed to `consumed == maxAssets` via normal fills (or via `setConsumed`).
3. Attacker calls `take(units=1, ...)` repeatedly.
4. Each call: `buyerAssets = 0`, `consumed` unchanged, cap check passes, but `creditOf`, `debtOf`, and `totalUnits` all increase by 1 unit (lines 408–417).
5. This continues indefinitely with no revert.

**Why existing checks fail:** The sole guard is `require(newConsumed <= offer.maxAssets)`. Since `newConsumed` never increases when `buyerAssets = 0`, this check is permanently satisfied. No other check bounds the number of unit-level fills.

**Protocol acknowledgment:** The NatSpec at line 94 states: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* The test `testBugBuyMaxAssetsBypass` (lines 857–889 of `test/TakeTest.sol`) explicitly confirms: after pre-consuming the offer to `maxAssets`, a `take(1, ...)` succeeds, `consumed` stays at `maxAssets`, but `creditOf`, `debtOf`, and `totalUnits` all strictly increase. The test name itself contains "Bug," confirming the protocol team treats this as an unresolved defect.

## Impact Explanation
The maker's `maxAssets` cap is intended to bound total economic exposure in asset terms. Due to rounding, an attacker can create unbounded credit and debt on behalf of the maker beyond what `maxAssets / buyerPrice` would imply. The maker's offer is overfilled in unit terms while the asset-denominated `consumed` counter never exceeds `maxAssets`, so no revert ever occurs. This constitutes unauthorized state change — unbounded debt/credit creation — against the maker's expressed intent, violating the core invariant that offers cannot be overfilled beyond their cap. The attacker bears the cost of the taker-side debt, making this a griefing vector against the maker's position integrity.

## Likelihood Explanation
Preconditions are easily satisfied: any buy offer at a tick above `MAX_TICK/2` (i.e., `buyerPrice < WAD`) with `maxAssets > 0` is vulnerable. The attacker requires no special privilege — any address can call `take()` as the taker. The attack is repeatable in a single transaction via callback or across multiple transactions. Ticks above `MAX_TICK/2` are common for lenders offering below-par rates, making this a realistic and broadly applicable condition.

## Recommendation
For buy offers with `maxAssets > 0`, require that `buyerAssets > 0` before proceeding, or use `mulDivUp` instead of `mulDivDown` when computing the `consumed` increment so that any nonzero `units` always advances the counter by at least 1. A minimal fix:

```solidity
if (offer.maxAssets > 0) {
    uint256 consumedDelta = offer.buy
        ? units.mulDivUp(buyerPrice, WAD)   // round UP for cap accounting
        : sellerAssets;
    newConsumed = consumed[offer.maker][offer.group] += consumedDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, add `require(buyerAssets > 0 || offer.maxAssets == 0, ZeroAssets())` before the cap check to reject zero-asset takes outright when an asset cap is active.

## Proof of Concept
The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` lines 857–889 is a complete, passing reproduction:

1. Create a buy offer with `maxAssets = 1` and `tick = MAX_TICK - 16` (so `buyerPrice < WAD`).
2. Pre-consume the offer to `consumed == maxAssets` via `setConsumed`.
3. Call `take(1, borrower, lenderOffer)`.
4. Assert `buyerAssets == 0`, `consumed` unchanged at `maxAssets`, yet `creditOf(lender) > before`, `debtOf(borrower) > before`, `totalUnits > before`.

All four assertions pass, confirming the cap is bypassed and position state mutates without bound.