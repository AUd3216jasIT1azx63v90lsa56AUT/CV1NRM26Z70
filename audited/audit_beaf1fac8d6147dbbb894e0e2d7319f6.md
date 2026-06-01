### Title
Buy-offer `maxAssets` cap bypassed via zero-rounding of `buyerAssets` when `offerPrice < WAD` - (File: src/Midnight.sol)

### Summary
When `offer.buy=true` and `offerPrice < WAD`, `buyerAssets` is computed as `units.mulDivDown(buyerPrice, WAD)`, which floors to zero for any `units < WAD/buyerPrice`. Because `consumed` is incremented by `buyerAssets` (not by `units`), a taker can call `take` with such small `units` values indefinitely without ever advancing `consumed`, bypassing the maker's `maxAssets` cap entirely. The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` explicitly reproduces and names this as a bug.

### Finding Description
**Exact code path** — `src/Midnight.sol` lines 363–369:

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
...
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

For a buy offer, `buyerPrice = offerPrice` (since `sellerPrice = offerPrice - fee` and `buyerPrice = sellerPrice + fee`). When `offerPrice < WAD`, any `units` satisfying `units * offerPrice < WAD` produces `buyerAssets = 0` via floor division. The consumed accumulator is then incremented by zero, so `newConsumed` never grows, and the `ConsumedAssets` guard never fires regardless of how many times `take` is called.

**Attacker-controlled inputs:**
- `offer.buy = true`, `offer.maxAssets = N > 0`, `offer.tick` chosen so `tickToPrice(tick) < WAD`
- `units` chosen in `[1, WAD/buyerPrice - 1]` so that `units * buyerPrice < WAD`

**Exploit flow:**
1. Maker (lender) publishes a buy offer with `maxAssets = N` and a tick where `offerPrice < WAD` (e.g., `MAX_TICK - 16` in the test).
2. Taker (borrower) calls `take(units=1, ...)` repeatedly.
3. Each call: `buyerAssets = 1 * offerPrice / WAD = 0`; `consumed += 0`; cap check passes.
4. Each call still executes the full position update: maker's credit increases by `units`, taker's debt increases by `units`, `totalUnits` grows.
5. No tokens are transferred (`buyerAssets = 0`, `sellerAssets = 0`), so the maker pays nothing and the taker receives nothing, yet the maker accumulates unbounded credit and the taker accumulates unbounded debt.

**Why existing checks fail:** The `require(newConsumed <= offer.maxAssets)` guard is correct in structure but operates on `buyerAssets`, not `units`. When `buyerAssets = 0`, the guard is a no-op. `EcrecoverRatifier.isRatified` only validates the Merkle proof and signature over the offer struct; it does not inspect `units` or `buyerAssets` and cannot prevent this.

### Impact Explanation
The maker's `maxAssets` cap — intended to bound total buyer-asset exposure — is rendered ineffective. A taker can fill the offer for an unbounded number of units (each fill with `units < WAD/buyerPrice`), causing the maker to accumulate credit far beyond their intended limit and the taker to accumulate matching debt with zero loan-token receipt. The invariant "offers cannot be overfilled" is violated: `consumed` stays at or below `maxAssets` while actual filled units grow without bound.

### Likelihood Explanation
**Preconditions:** (1) `offer.buy = true`; (2) `offer.maxAssets > 0`; (3) `offerPrice < WAD` — achievable at any tick below the WAD-price tick, which is a normal part of the tick range; (4) taker chooses `units = 1` (or any value `< WAD/offerPrice`). All preconditions are reachable by an unprivileged taker with no special access. The attack is repeatable in a loop within a single transaction via `multicall`. The protocol's own test suite (`testBugBuyMaxAssetsBypass`) confirms the path is live and passes.

### Recommendation
Add a guard that rejects a non-zero `units` take that produces zero `buyerAssets` (or `sellerAssets`) in assets mode:

```solidity
if (offer.maxAssets > 0) {
    uint256 assetsDelta = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || assetsDelta > 0, ZeroAssetsTake());
    newConsumed = consumed[offer.maker][offer.group] += assetsDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

This ensures that any fill with `units > 0` must advance `consumed` by at least 1, making the cap meaningful. Alternatively, track consumed in `units` regardless of mode and convert `maxAssets` to a units ceiling at offer-creation time, eliminating the rounding gap entirely.

### Proof of Concept
The existing Foundry unit test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` (lines 857–889) is a complete, passing PoC. A fuzz/invariant extension:

```solidity
function testFuzz_BuyMaxAssetsBypassUnboundedUnits(uint256 numFills) public {
    numFills = bound(numFills, 1, 1000);
    lenderOffer.maxUnits = 0;
    lenderOffer.maxAssets = 1;
    lenderOffer.tick = MAX_TICK - 16; // offerPrice < WAD → buyerAssets=0 for units=1

    collateralize(market, borrower, numFills);

    uint256 debtBefore = midnight.debtOf(id, borrower);
    for (uint256 i; i < numFills; i++) {
        take(1, borrower, lenderOffer);
    }

    // consumed never exceeded maxAssets:
    assertLe(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets);
    // but units filled = numFills >> 0, violating the cap's intent:
    assertEq(midnight.debtOf(id, borrower) - debtBefore, numFills);
    assertGt(numFills, 0); // cap was bypassed
}
```

**Expected assertions:** `consumed == 1 == maxAssets` throughout all fills; `debtOf(borrower)` grows by `numFills`; `creditOf(lender)` grows by `numFills`; no token transfers occur. [1](#0-0) [2](#0-1)

### Citations

**File:** src/Midnight.sol (L363-369)
```text
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
