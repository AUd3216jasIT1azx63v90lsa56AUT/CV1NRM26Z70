Audit Report

## Title
`maxAssets` cap bypass via `mulDivDown` zero-rounding on buy offers — (`File: src/Midnight.sol`)

## Summary
In `take()`, when `offer.buy == true` and `offer.maxAssets > 0`, the consumed tracking increments by `buyerAssets = units.mulDivDown(buyerPrice, WAD)`. When `units * buyerPrice < WAD`, this rounds to zero, leaving `consumed` unchanged and allowing the `require(newConsumed <= offer.maxAssets)` guard to pass unconditionally. Any unprivileged taker can call `take()` repeatedly with small `units`, accumulating unbounded credit for the maker and debt for themselves while the offer's `maxAssets` cap is never enforced.

## Finding Description
**Exact code path — `src/Midnight.sol` lines 358–369:**

```solidity
uint256 offerPrice = TickLib.tickToPrice(offer.tick);
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
uint256 buyerPrice  = sellerPrice + _settlementFee;                          // == offerPrice
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // rounds DOWN → 0 when units*offerPrice < WAD
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy
        ? buyerAssets   // += 0 → consumed never advances
        : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());  // trivially passes
}
```

**Root cause:** `buyerPrice` simplifies to `offerPrice` (the `_settlementFee` terms cancel). For any tick below `MAX_TICK`, `offerPrice < WAD`. Choosing `units` such that `units * offerPrice < WAD` (e.g., `units = 1` for most realistic prices) makes `buyerAssets = 0`. The `consumed` mapping is not incremented, so the cap check is permanently satisfied regardless of how many times `take()` is called.

**Why existing checks fail:** The sole guard is `require(newConsumed <= offer.maxAssets)`. Because `newConsumed` never increases past its starting value, this check never triggers. No other mechanism bounds the number of fills or total units transferred.

**Protocol acknowledgement:** The NatSpec at line 94 states: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* The test `testBugBuyMaxAssetsBypass` (lines 858–888) pre-fills `consumed` to `maxAssets`, calls `take(1, ...)`, and asserts that `creditOf`, `debtOf`, and `totalUnits` all increase while `consumed` remains at `maxAssets` and no tokens move.

## Impact Explanation
The maker's `maxAssets` cap — their explicit authorization boundary for position size — is completely bypassed. A taker can force the maker into an arbitrarily large credit position and accumulate corresponding debt for themselves, both without any token transfer (since `buyerAssets = sellerAssets = 0`). This violates the core accounting invariant that offers cannot be overfilled beyond their stated cap. The maker's risk exposure (credit position, pending fees, `totalUnits` contribution) grows without bound beyond what they authorized.

## Likelihood Explanation
- Requires `offerPrice < WAD`, satisfied by every tick below `MAX_TICK` — the common case for any discounted market.
- Requires `units * offerPrice < WAD`; for tokens with 8 decimals (e.g., WBTC), `units = 1` is sufficient across a wide price range.
- No privileged access, no oracle manipulation, no victim cooperation needed. Any external account can execute this against any qualifying buy offer.
- Repeatable indefinitely within a single transaction via multicall or across multiple transactions.

## Recommendation
Replace `mulDivDown` with `mulDivUp` for `buyerAssets` when computing the consumed increment on buy offers, consistent with how `sellerAssets` is computed for sell offers. Alternatively, add an explicit guard: `require(buyerAssets > 0 || units == 0, ZeroAssets())` before the consumed update. A stricter fix would require `units` to be large enough that `buyerAssets >= 1` whenever `maxAssets > 0`.

## Proof of Concept
The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` lines 858–888 is a complete, passing reproduction:
1. Create a buy offer with `maxAssets = 1`, `tick = MAX_TICK - 16` (so `offerPrice < WAD`).
2. Pre-fill `consumed` to `maxAssets` via `setConsumed`.
3. Call `take(1, borrower, lenderOffer)`.
4. Assert `buyerAssets == 0`, `consumed` unchanged at `maxAssets`, but `creditOf(lender)`, `debtOf(borrower)`, and `totalUnits` all strictly increased. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** src/Midnight.sol (L94-94)
```text
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L358-369)
```text
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
        uint256 _settlementFee = settlementFee(id, timeToMaturity);
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** test/TakeTest.sol (L858-888)
```text
    function testBugBuyMaxAssetsBypass() public {
        deal(address(loanToken), lender, 0); // lender pays 0
        collateralize(market, borrower, 100);

        lenderOffer.maxUnits = 0;
        lenderOffer.maxAssets = 1;
        lenderOffer.tick = MAX_TICK - 16; // offerPrice < WAD

        // Fully consume the offer before the take.
        vm.prank(lender);
        midnight.setConsumed(lenderOffer.group, lenderOffer.maxAssets, lender);

        uint256 lenderCreditBefore = midnight.creditOf(id, lender);
        uint256 borrowerDebtBefore = midnight.debtOf(id, borrower);
        uint256 totalUnitsBefore = midnight.totalUnits(id);
        uint256 lenderBalBefore = loanToken.balanceOf(lender);
        uint256 borrowerBalBefore = loanToken.balanceOf(borrower);

        (uint256 buyerAssets, uint256 sellerAssets) = take(1, borrower, lenderOffer);

        assertEq(buyerAssets, 0);
        assertEq(sellerAssets, 0);

        // Nothing observable to the cap or token balances changed:
        assertEq(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets);
        assertEq(loanToken.balanceOf(lender), lenderBalBefore);
        assertEq(loanToken.balanceOf(borrower), borrowerBalBefore);
        // But position state strictly changed:
        assertGt(midnight.creditOf(id, lender), lenderCreditBefore);
        assertGt(midnight.debtOf(id, borrower), borrowerDebtBefore);
        assertGt(midnight.totalUnits(id), totalUnitsBefore);
```
