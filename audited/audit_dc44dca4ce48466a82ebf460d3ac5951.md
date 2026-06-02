Audit Report

## Title
Buy Offer `maxAssets` Cap Bypassed in Unit Terms via `mulDivDown` Rounding to Zero - (File: `src/Midnight.sol`)

## Summary
When a buy offer uses `maxAssets` as its fill cap, the consumed counter is incremented by `buyerAssets = units.mulDivDown(buyerPrice, WAD)`. At any sub-WAD tick where `units * buyerPrice < WAD`, `mulDivDown` returns 0, so the `ConsumedAssets` guard is a no-op while `credit`, `debt`, and `totalUnits` are updated by the full `units` amount. This allows unbounded unit-denominated position accumulation beyond the maker's intended asset cap, corrupting market-wide accounting.

## Finding Description
**Root cause** — `src/Midnight.sol`, `take()`, lines 363–373:

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
// ...
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

For a buy offer, `buyerPrice = tickToPrice(tick) + settlementFee`. Since `tickToPrice` returns values ≤ WAD, `buyerPrice < WAD` is the normal operating condition. When `units * buyerPrice < WAD`, `mulDivDown` returns 0, so `consumed` is incremented by 0 and the `require(newConsumed <= offer.maxAssets)` check passes trivially regardless of how many units are filled.

Despite the zero asset increment, lines 408–417 still apply the full `units` to positions:

```solidity
buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);
sellerPos.debt   += UtilsLib.toUint128(sellerDebtIncrease);
_marketState.totalUnits += buyerCreditIncrease - sellerCreditDecrease;
```

**Existing guard failure**: The `ConsumedAssets` guard is purely asset-denominated. When `buyerAssets = 0`, it cannot enforce any unit-level limit and is structurally bypassed.

**Protocol acknowledgment**: The protocol documents this at line 94: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."*

**Confirmed by existing test** — `test/TakeTest.sol`, `testBugBuyMaxAssetsBypass`, lines 858–889: the test pre-fills `consumed` to `maxAssets`, calls `take(units=1)`, and asserts that `consumed` stays at `maxAssets` and token balances are unchanged, while `creditOf`, `debtOf`, and `totalUnits` all strictly increase.

## Impact Explanation
A maker who posts a buy offer with `maxAssets = N` intends to cap their total credit exposure at `N` loan tokens. An attacker can call `take(units=1)` in a loop, accumulating arbitrarily large credit/debt positions while the consumed counter never exceeds `N`. The market-wide `totalUnits` is inflated without any asset backing, corrupting solvency accounting and loss factor calculations that depend on it. This is a direct accounting integrity failure: phantom credit/debt positions are created that have no corresponding token collateral, which can affect settlement outcomes for all market participants.

## Likelihood Explanation
Preconditions are minimal: any buy offer with `maxAssets > 0` and `buyerPrice < WAD` (satisfied at all sub-WAD ticks, which is the normal case). No special privileges are required — any `msg.sender` can act as taker directly per line 346 (`taker == msg.sender`). The condition `units * buyerPrice < WAD` is trivially satisfied with `units = 1` at any realistic price. The attack is repeatable indefinitely and costs only gas.

## Recommendation
Add a unit-denominated floor check when `maxAssets > 0`: if `buyerAssets == 0` and `units > 0`, revert (or require `buyerAssets > 0` before proceeding). Alternatively, track both asset and unit consumption and enforce both caps independently. A minimal fix:

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
if (offer.maxAssets > 0) {
    require(buyerAssets > 0, ZeroAssetTake()); // prevent rounding-to-zero bypass
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

## Proof of Concept
The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` (lines 858–889) is a complete, passing reproduction:

1. Set `lenderOffer.maxAssets = 1`, `lenderOffer.tick = MAX_TICK - 16` (sub-WAD price).
2. Pre-fill `consumed` to `maxAssets` via `midnight.setConsumed(...)`.
3. Call `take(units=1, borrower, lenderOffer)`.
4. Assert `buyerAssets == 0`, `sellerAssets == 0`, `consumed` unchanged at `maxAssets`, token balances unchanged.
5. Assert `creditOf(id, lender) > lenderCreditBefore`, `debtOf(id, borrower) > borrowerDebtBefore`, `totalUnits(id) > totalUnitsBefore`.

All assertions pass, confirming the bypass is real and reproducible.