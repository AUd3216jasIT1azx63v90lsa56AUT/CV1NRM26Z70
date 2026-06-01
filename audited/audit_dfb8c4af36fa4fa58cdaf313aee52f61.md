### Title
`maxAssets` Cap Bypass via `mulDivDown` Zero-Rounding on Buy Offers - (File: `src/Midnight.sol`)

### Summary

In `take`, when `offer.buy == true` and `offer.maxAssets > 0`, the consumed tracker is incremented by `buyerAssets = units.mulDivDown(buyerPrice, WAD)`. Because `mulDivDown` truncates toward zero, any call with `units * buyerPrice < WAD` produces `buyerAssets = 0`, leaving `consumed[offer.maker][offer.group]` unchanged. An unprivileged taker can therefore call `take` with arbitrarily many small-`units` fills — each passing the `ConsumedAssets` guard — while the maker's credit and the taker's debt grow without bound, completely defeating the `maxAssets` cap.

### Finding Description

**Exact code path** — `src/Midnight.sol` lines 363–369:

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // rounds DOWN → can be 0
    : units.mulDivUp(buyerPrice, WAD);

uint256 newConsumed;
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy
        ? buyerAssets   // ← 0 when units * buyerPrice < WAD
        : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

**Price range that enables the attack**: `tickToPrice(0) == 0` and `tickToPrice(2) == 1e12` (the minimum non-zero representable price). For any tick where `offerPrice < WAD`, a taker can choose `units` small enough that `units * offerPrice < WAD`, making `buyerAssets = 0`.

**Exploit flow**:
1. Maker posts a buy offer with `offer.buy = true`, `offer.maxAssets = M`, and `offer.tick` set to any tick below `MAX_TICK` (i.e., `offerPrice < WAD`).
2. Taker calls `take` repeatedly with `units` chosen so that `units * buyerPrice < WAD` (e.g., `units = 1` when `buyerPrice = 1e12`).
3. Each call: `buyerAssets = 0`, `consumed` does not change, `require(newConsumed <= maxAssets)` always passes.
4. Each call still executes the full position update: maker's `credit` increases by `units`, taker's `debt` increases by `units`, `totalUnits` grows.
5. After N calls, total `units` transferred = N, total `consumed` = 0 (or whatever it was before), and the maker's credit exposure is unbounded.

**Why existing checks fail**: The `ConsumedAssets` guard at line 369 only fires when `newConsumed > maxAssets`. Since `newConsumed` never increases (because `buyerAssets = 0`), the guard is permanently inert for these fills. There is no `require(units == 0 || buyerAssets > 0)` guard anywhere in the path.

**Confirmed by the codebase's own test** — `test/TakeTest.sol` lines 858–889, function `testBugBuyMaxAssetsBypass`, which pre-fills `consumed` to `maxAssets = 1`, then takes `units = 1` at `tick = MAX_TICK - 16` (where `offerPrice < WAD`), and asserts that `consumed` is unchanged while `creditOf`, `debtOf`, and `totalUnits` all strictly increased.

### Impact Explanation

The `maxAssets` cap on buy offers is completely ineffective when `buyerPrice < WAD` and the taker uses sub-WAD unit fills. The maker's intended exposure limit is bypassed: the maker accumulates unbounded credit (and the taker unbounded debt) at zero token cost per fill, since `buyerAssets = 0` means no loan tokens are transferred either. This violates the core invariant that "offers cannot be overfilled."

### Likelihood Explanation

- **Preconditions**: Any buy offer with `maxAssets > 0` and `tick < MAX_TICK` (i.e., `offerPrice < WAD`). This covers virtually all real buy offers, since `MAX_TICK` (price = 1.0) is the par-value boundary.
- **Attacker**: Any unprivileged taker; no special role or capital required (token cost per fill is 0).
- **Repeatability**: Unlimited; each fill is independent and the guard never trips.
- **Feasibility**: Trivial — the taker simply passes `units = 1` (or any value below `WAD / buyerPrice`) on each call.

### Recommendation

Add a guard that rejects non-zero `units` fills that produce zero `buyerAssets` (or `sellerAssets`) when the offer uses `maxAssets` mode:

```solidity
if (offer.maxAssets > 0) {
    uint256 assetsDelta = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || assetsDelta > 0, ZeroAssetFill());
    newConsumed = consumed[offer.maker][offer.group] += assetsDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

This ensures that every economically meaningful fill (i.e., `units > 0`) registers at least 1 unit of consumed assets, making the cap enforceable.

### Proof of Concept

```solidity
// Foundry unit test (mirrors testBugBuyMaxAssetsBypass in test/TakeTest.sol)
function testMaxAssetsBypassFuzz(uint256 N) public {
    N = bound(N, 1, 1000);

    // Buy offer: maker = lender, maxAssets = 1, tick below MAX_TICK so offerPrice < WAD
    lenderOffer.maxUnits = 0;
    lenderOffer.maxAssets = 1;
    lenderOffer.tick = MAX_TICK - 16; // offerPrice < WAD → 1.mulDivDown(price, WAD) == 0

    collateralize(market, borrower, N);
    deal(address(loanToken), lender, 0);

    uint256 creditBefore = midnight.creditOf(id, lender);

    for (uint256 i = 0; i < N; i++) {
        (uint256 buyerAssets,) = take(1, borrower, lenderOffer);
        // Each fill: buyerAssets == 0, consumed unchanged, guard never fires
        assertEq(buyerAssets, 0);
        assertEq(midnight.consumed(lender, lenderOffer.group), 0);
    }

    // Invariant violated: credit grew by N despite maxAssets = 1
    assertEq(midnight.creditOf(id, lender), creditBefore + N);
    // consumed never reached maxAssets
    assertLt(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets + 1);
}
```

**Expected assertions**: `creditOf(lender)` increases by `N`; `consumed` stays at 0; no revert. This directly falsifies the invariant `sum(buyerAssets) <= offer.maxAssets` in terms of economic units transferred. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** src/libraries/TickLib.sol (L44-52)
```text
    function tickToPrice(uint256 tick) internal pure returns (uint256) {
        require(tick <= MAX_TICK, TickOutOfRange());
        unchecked {
            // forge-lint: disable-next-item(unsafe-typecast)
            return uint256(1e36)
                    .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
                    .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
        }
    }
```

**File:** test/TickLibTest.sol (L15-19)
```text
    function testTickToPriceMinMax() public pure {
        assertEq(TickLib.tickToPrice(0), 0, "tick 0");
        assertEq(TickLib.tickToPrice(2), 1e12, "first non-zero tick");
        assertEq(TickLib.tickToPrice(MAX_TICK - 2), 1e18 - 1e12, "tick max - 2 just below par");
        assertEq(TickLib.tickToPrice(MAX_TICK), 1e18, "tick max");
```
