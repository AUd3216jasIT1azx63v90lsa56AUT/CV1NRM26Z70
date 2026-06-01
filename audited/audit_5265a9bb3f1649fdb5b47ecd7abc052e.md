### Title
Buy-offer `maxAssets` cap bypassed via `mulDivDown` zero-rounding when `buyerPrice < WAD` - (`File: src/Midnight.sol`)

### Summary

For a buy offer (`offer.buy = true`) with `maxAssets > 0`, the consumed tracking increments by `units.mulDivDown(buyerPrice, WAD)`. When `buyerPrice < WAD` (any tick below `MAX_TICK`) and `units = 1`, this expression evaluates to `0`, so `consumed[maker][group]` never increases. A taker can therefore call `take` with `units = 1` an unbounded number of times, each time passing the `require(newConsumed <= offer.maxAssets)` check, delivering units to the maker and accruing debt on themselves with zero asset transfer. The codebase itself acknowledges and demonstrates this exact behavior.

### Finding Description

**Code path** — `src/Midnight.sol`, `take()`:

```
line 363: buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : ...
line 368: newConsumed = consumed[offer.maker][offer.group] += buyerAssets;   // += 0
line 369: require(newConsumed <= offer.maxAssets, ConsumedAssets());          // trivially passes
```

**Root cause** — `buyerPrice = offerPrice = TickLib.tickToPrice(offer.tick)`. Since `tickToPrice` always returns a value `<= WAD`, and for any tick below `MAX_TICK` the price is strictly `< WAD`, the expression `1.mulDivDown(buyerPrice, WAD) = floor(buyerPrice / WAD) = 0`. The consumed accumulator therefore does not advance, and the cap check is permanently satisfied regardless of how many unit-sized takes are executed.

**Attacker-controlled inputs** — `units = 1` (or any value small enough that `units * buyerPrice < WAD`), `offer.tick` set to any tick below `MAX_TICK` (attacker is the taker; the offer is the maker's).

**Exploit flow**:
1. Maker (lender) posts a buy offer with `maxAssets = N`, `tick < MAX_TICK` (i.e., `buyerPrice < WAD`).
2. Taker calls `take(offer, ..., units=1, ...)` repeatedly.
3. Each call: `buyerAssets = 0`, `consumed` unchanged, cap check passes, lender credit increases by 1, borrower debt increases by 1, zero loan tokens transferred.
4. After `k` calls the maker holds `k` units of credit while having paid 0 assets; `consumed` remains at its pre-take value.

**Why existing checks fail** — The `require(newConsumed <= offer.maxAssets)` check at line 369 is the sole cap enforcement. It is defeated because the increment is `buyerAssets`, not `units`, and `buyerAssets` rounds to zero for sub-WAD prices at unit granularity. The Certora rule `takeConsumedBoundedByMax` in `certora/specs/Consume.spec` (line 62) uses `NONDET` summaries for `mulDivDown`/`mulDivUp` and therefore does not model this rounding collapse.

**Codebase confirmation** — The protocol's own comment at `src/Midnight.sol` line 94 states: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* The test `testBugBuyMaxAssetsBypass` (`test/TakeTest.sol` lines 858–889) explicitly demonstrates: a buy offer with `maxAssets = 1` and `tick = MAX_TICK - 16` (price < WAD), already fully consumed (`consumed = 1`), is successfully taken with `units = 1`; `buyerAssets = 0`, `consumed` stays at `1`, yet lender credit and borrower debt both increase.

### Impact Explanation

The `maxAssets` cap on a buy offer is rendered ineffective for any `buyerPrice < WAD`. A taker can deliver an unbounded number of units to the maker (buyer/lender) without the maker paying any loan tokens, violating the invariant that `consumed[maker][group]` accurately tracks total buyer asset exposure and that offers cannot be overfilled. The maker accrues credit beyond what `maxAssets` permits; the taker accrues matching debt with zero compensation — a griefing vector against the taker and a cap-bypass against the maker's stated intent.

### Likelihood Explanation

**Preconditions**: buy offer with `maxAssets > 0` and any tick below `MAX_TICK` (i.e., virtually every realistic buy offer, since `MAX_TICK` corresponds to price = WAD = 1.0, the maximum possible price). No special permissions required; any address can be the taker. **Repeatability**: unlimited — each 1-unit take costs only gas. The attack is trivially scriptable.

### Recommendation

Track consumed in units for the cap check, or use `mulDivUp` (rounding against the taker) when computing the consumed increment for buy offers in assets mode:

```solidity
// Option A: use mulDivUp for the consumed delta so rounding never collapses to zero
uint256 consumedDelta = offer.buy
    ? units.mulDivUp(buyerPrice, WAD)   // was mulDivDown
    : units.mulDivUp(sellerPrice, WAD);
newConsumed = consumed[offer.maker][offer.group] += consumedDelta;
require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

This preserves the maker-favorable rounding for the actual asset transfer (`buyerAssets` stays `mulDivDown`) while ensuring the cap accounting never under-counts.

### Proof of Concept

```solidity
// Foundry unit test (extend TakeTest)
function testBuyMaxAssetsOverfillViaRounding() public {
    // Buy offer: lender is maker/buyer, buyerPrice < WAD
    lenderOffer.maxUnits = 0;
    lenderOffer.maxAssets = 100;          // maker intends to buy at most 100 assets
    lenderOffer.tick = MAX_TICK - 16;     // buyerPrice < WAD → 1.mulDivDown(price,WAD) = 0

    deal(address(loanToken), lender, 0);  // lender pays nothing
    collateralize(market, borrower, 200); // borrower can take on debt

    // Take 150 units one at a time; each take: buyerAssets=0, consumed unchanged
    for (uint256 i; i < 150; i++) {
        take(1, borrower, lenderOffer);
    }

    // consumed never exceeded maxAssets, yet 150 units were delivered
    assertLe(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets);
    assertEq(midnight.creditOf(id, lender), 150);   // FAILS expected invariant
    assertEq(midnight.debtOf(id, borrower), 150);

    // Assert the invariant the protocol should hold:
    // total units taken * buyerPrice / WAD <= maxAssets
    uint256 price = TickLib.tickToPrice(lenderOffer.tick);
    uint256 impliedMaxUnits = uint256(lenderOffer.maxAssets).mulDivUp(WAD, price);
    assertLe(midnight.creditOf(id, lender), impliedMaxUnits); // will FAIL: 150 > impliedMaxUnits
}
```

Expected: the final `assertLe` fails, proving that the maker received more units than `maxAssets` implies. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** src/Midnight.sol (L89-94)
```text
/// OFFER CAPS
/// @dev At most one of maxAssets or maxUnits can be nonzero per offer.
/// @dev maxAssets caps max buyer assets if offer.buy is true, and caps max seller assets otherwise.
/// @dev If maxAssets > 0, assets are capped to maxAssets, otherwise units are capped to maxUnits.
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

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

**File:** certora/specs/Consume.spec (L58-64)
```text
/// After a successful take, consumed[offer.maker][offer.group] does not exceed the effective max.
rule takeConsumedBoundedByMax(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert offer.maxAssets > 0 => consumed(offer.maker, offer.group) <= offer.maxAssets;
    assert offer.maxAssets == 0 => consumed(offer.maker, offer.group) <= offer.maxUnits;
}
```
