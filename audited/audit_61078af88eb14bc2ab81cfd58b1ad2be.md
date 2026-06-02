Audit Report

## Title
`consumed` never increments for assets-mode buy offers at tick=0 with zero settlement fee, enabling unlimited offer reuse — (File: src/Midnight.sol)

## Summary
When `offer.maxAssets > 0`, `offer.buy == true`, `offer.tick == 0`, and `_settlementFee == 0`, `tickToPrice(0)` evaluates to exactly `0` after `PRICE_ROUNDING_STEP` truncation, causing `buyerAssets = 0`. The `consumed` counter is incremented by `buyerAssets` (not `units`), so it never advances, the `ConsumedAssets` guard trivially passes on every call, and the offer can be taken an unlimited number of times regardless of `maxAssets`. Each successful take reduces the maker's debt by `units` with zero token transfer.

## Finding Description

**`tickToPrice(0) = 0` — `src/libraries/TickLib.sol:44-52`:**

`tickToPrice(0)` computes:
```
1e36 / (1e18 + wExp(LN_ONE_PLUS_DELTA * 2910))
≈ 1e36 / (1e18 + 2e24)
≈ 1e36 / 2e24
= 5e11
```
Then `divHalfDownUnchecked(5e11, PRICE_ROUNDING_STEP=1e12)` = `(5e11 + 4.999...e11) / 1e12 = 0` (integer division truncates). Final result: `0 * 1e12 = 0`.

**Asset computation — `src/Midnight.sol:361-364`:**
```solidity
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;  // 0 - 0 = 0
uint256 buyerPrice  = sellerPrice + _settlementFee;                           // 0 + 0 = 0
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : ...;   // mulDivDown(U, 0, WAD) = 0
```

**Consumed accounting — `src/Midnight.sol:367-369`:**
```solidity
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```
`consumed += 0` → no change. `require(unchanged_value <= maxAssets)` always passes.

**Exploit flow:**
1. Maker creates a buy offer: `tick=0`, `maxAssets > 0`, `_settlementFee=0` (default for new market), maker holds `buyerPos.debt >= units`.
2. Taker (cooperating address) calls `take(offer, ..., units=U)`.
3. `buyerPrice = 0` → `buyerAssets = 0` → `consumed` unchanged → guard passes.
4. `buyerCreditIncrease = zeroFloorSub(U, debt) = 0` → `buyerPos.debt -= U` (maker's debt silently reduced).
5. `sellerAssets = 0` → both `safeTransferFrom` calls transfer 0 tokens.
6. Taker must remain healthy (line 476 check), but no token cost to either party.
7. Steps 2–6 repeat indefinitely; `consumed` never reaches `maxAssets`.

**Why existing checks fail:**
- `require(newConsumed <= offer.maxAssets)`: `newConsumed` is unchanged, so this always passes regardless of how many takes occur.
- The Certora `takeConsumedDelta` rule (`Consume.spec:67-75`) explicitly requires `offer.maxAssets == 0` and has no equivalent for assets mode.
- The `takeConsumedBoundedByMax` rule (`Consume.spec:59-64`) is sound in theory but the spec summarizes `mulDivDown` as `NONDET`, so the prover cannot detect the zero-price edge case.
- The protocol comment acknowledging "It is possible to give units to a fully consumed assets-based buy offer with price < 1" addresses only the already-at-max case, not indefinite reuse of a non-exhausted offer.

## Impact Explanation
The `maxAssets` consumption cap — the primary mechanism for limiting offer exposure in assets mode — is completely bypassed for buy offers at tick=0 with zero settlement fee. A maker with existing debt can have that debt reduced to zero by a cooperating taker (or a second address the maker controls) with zero token transfer and zero consumed increment per call. This violates the core protocol invariant that an offer with `maxAssets = N` cannot be filled beyond `N` assets. The attack is executable within a single transaction via multicall or across multiple transactions, and the `consumed` mapping permanently misrepresents the offer's fill state.

## Likelihood Explanation
All preconditions are reachable without privilege:
- `offer.tick = 0` is a valid tick (passes `tick % spacing == 0` for any spacing dividing `MAX_TICK`).
- `offer.maxAssets > 0` is a standard offer configuration.
- `_settlementFee = 0` is the default state for a freshly created market (no fee accrual yet).
- Maker holding debt in the same market is a normal borrower state.
- The taker only needs to be a distinct address from the maker and must remain healthy.

The attack is repeatable in a loop within a single transaction via multicall.

## Recommendation
In the assets-mode branch, increment `consumed` by `units` rather than by the asset amount, or add a minimum-units guard:

```solidity
if (offer.maxAssets > 0) {
    // Use units as the consumed unit to prevent zero-increment bypass
    uint256 assetIncrement = offer.buy ? buyerAssets : sellerAssets;
    require(assetIncrement > 0 || units == 0, ZeroAssetTake());
    newConsumed = consumed[offer.maker][offer.group] += assetIncrement;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```
Alternatively, require `units > 0` implies `buyerAssets > 0` (i.e., reject takes where the computed asset amount rounds to zero) by adding `require(units == 0 || (offer.buy ? buyerAssets : sellerAssets) > 0, ZeroAssetTake())` before the consumed update. Additionally, add a Certora rule analogous to `takeConsumedDelta` for the assets-mode path that does not summarize `mulDivDown` as `NONDET`.

## Proof of Concept
```solidity
// Foundry test sketch
function testConsumedBypassAtTickZero() public {
    // 1. Create market with settlementFee = 0 (fresh market, no fee)
    // 2. Maker deposits collateral, borrows units (creates debt)
    // 3. Maker creates buy offer: tick=0, maxAssets=1e6, buy=true
    // 4. Taker (separate address with collateral) calls take() with units=1e18
    //    → assert buyerAssets == 0
    //    → assert consumed[maker][group] == 0 (unchanged)
    // 5. Repeat take() 100 times
    //    → assert consumed[maker][group] == 0 still
    //    → assert maker.debt reduced by 100 * 1e18
    // 6. Confirm offer was never "exhausted" despite maxAssets=1e6
}
```
The existing test `testPriceZeroNoSettlementFeeSell` in `test/TakeTest.sol` already asserts `buyerAssets == 0` at tick=0 with `units = 1e18`, confirming the zero-price condition. Extending that test to loop `take()` calls and assert `consumed` remains zero would reproduce the finding directly.