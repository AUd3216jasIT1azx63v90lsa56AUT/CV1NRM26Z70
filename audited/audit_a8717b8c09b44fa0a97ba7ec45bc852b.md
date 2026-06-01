### Title
Buy-offer `consumed` accounting truncates to zero when `units * buyerPrice < WAD`, rendering `maxAssets` cap unenforceable - (File: src/Midnight.sol)

### Summary
When a buy offer has `maxAssets > 0` and a taker fills `units = 1` at any tick below `MAX_TICK` (i.e., `buyerPrice < WAD`), the expression `units.mulDivDown(buyerPrice, WAD)` evaluates to `floor(buyerPrice / WAD) = 0`. The `consumed` mapping is incremented by zero on every such fill, so `newConsumed` never approaches `maxAssets`, and the cap check `require(newConsumed <= offer.maxAssets)` is trivially satisfied forever.

### Finding Description
**Exact code path** — `src/Midnight.sol` lines 363 and 367–369:

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // ← floor(1 * buyerPrice / WAD) = 0 when buyerPrice < WAD
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());   // 0 <= maxAssets always passes
}
```

**Root cause** — `buyerAssets` is computed with `mulDivDown` (floor division). For a buy offer, `buyerPrice = tickToPrice(offer.tick)`, which is proven to be `<= WAD` for every valid tick (Certora `tickToPriceAtMostWad` rule) and equals exactly `WAD` only at `MAX_TICK`. For every other tick, `buyerPrice < WAD`, so `mulDivDown(1, buyerPrice, WAD) = 0`. The `consumed` delta is therefore 0, and the cap is never consumed.

**Attacker-controlled inputs:**
- `offer.buy = true`, `offer.maxAssets = X > 0`
- `offer.tick` = any tick below `MAX_TICK` (e.g., `MAX_TICK - 2` gives `buyerPrice = WAD - 1e12`)
- `units = 1` per call

**Exploit flow:**
1. Maker posts a buy offer with `maxAssets = X` at tick `< MAX_TICK`.
2. Taker calls `take(..., units=1, ...)` N times.
3. Each call: `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`; `consumed[maker][group] += 0`; `newConsumed = 0`; `require(0 <= X)` passes.
4. Maker's credit position increases by 1 unit per fill (line 410: `buyerPos.credit += toUint128(buyerCreditIncrease)`).
5. After N fills: `consumed[maker][group] == 0`, maker has N units of credit, offer cap never triggered.

**Why existing checks fail:**
- `require(newConsumed <= offer.maxAssets)` — trivially satisfied since `newConsumed` stays 0.
- The Certora `Consume.spec` rule `takeConsumedBoundedByMax` only asserts `consumed <= maxAssets`, which holds vacuously at 0; it does not assert that `consumed` must increase by a positive amount per non-zero fill.
- The `takeConsumedDelta` rule (delta = units) is scoped to `maxAssets == 0` only and does not cover the assets-mode branch. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
The `maxAssets` cap on a buy offer is completely unenforceable whenever `buyerPrice < WAD` and the taker uses `units = 1`. The maker accumulates unbounded credit beyond the intended limit. The `consumed` mapping — the sole on-chain record of fill activity for the offer group — remains at 0 regardless of how many fills occur, violating the core invariant that "offers cannot be overfilled" and that `consumed` monotonically tracks fill volume. [4](#0-3) 

### Likelihood Explanation
- **Preconditions:** Any buy offer with `maxAssets > 0` at any tick except `MAX_TICK` (i.e., the overwhelming majority of real offers, since `MAX_TICK` corresponds to price = 1 WAD = par value).
- **Feasibility:** The taker only needs to call `take` with `units = 1` repeatedly. No special privileges, no oracle manipulation, no flash loan required.
- **Taker cost:** For `units = 1` and `buyerPrice < WAD`, `sellerAssets = mulDivDown(1, sellerPrice, WAD) = 0` as well, so the taker receives 0 loan tokens but takes on 1 unit of debt per fill (requires collateral). This makes the attack a griefing vector rather than a direct profit extraction, but it fully bypasses the maker's stated cap.
- **Repeatability:** Unlimited; the cap is never consumed. [5](#0-4) [6](#0-5) 

### Recommendation
Enforce a minimum consumed increment of 1 per non-zero fill in assets mode. The simplest targeted fix is to require that a non-zero `units` input produces a non-zero asset amount before updating `consumed`:

```solidity
if (offer.maxAssets > 0) {
    uint256 assetsDelta = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || assetsDelta > 0, ZeroAssetFill()); // new guard
    newConsumed = consumed[offer.maker][offer.group] += assetsDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, enforce a minimum fill size at the call site (e.g., `require(units * buyerPrice >= WAD || units == 0)`), or document and enforce that `maxAssets` mode is only valid when `buyerPrice >= WAD` (i.e., `tick == MAX_TICK`). [4](#0-3) 

### Proof of Concept
```solidity
// Foundry unit test
function testBuyOfferMaxAssetsCapBypassedBySmallFills() public {
    // Setup: buy offer at tick MAX_TICK - 2 → buyerPrice = WAD - 1e12 < WAD
    uint256 tick = MAX_TICK - 2;
    // tickToPrice(MAX_TICK - 2) == 1e18 - 1e12  (confirmed by testTickToPriceMinMax)
    borrowerOffer.buy = true;
    borrowerOffer.tick = tick;
    borrowerOffer.maxAssets = 100;   // cap: 100 asset-units
    borrowerOffer.maxUnits = 0;

    uint256 N = 200;
    deal(address(loanToken), lender, N);
    collateralize(market, lender, N);   // taker needs collateral for debt

    for (uint256 i = 0; i < N; i++) {
        take(1, lender, borrowerOffer);  // units = 1 each time
    }

    // consumed stays 0 because mulDivDown(1, WAD-1e12, WAD) == 0
    assertEq(midnight.consumed(borrowerOffer.maker, borrowerOffer.group), 0,
        "consumed must be 0 — cap never triggered");

    // maker (buyer) credit grew by N despite maxAssets = 100
    assertEq(midnight.creditOf(id, borrowerOffer.maker), N,
        "maker credit grew unboundedly past maxAssets cap");
}
```

Expected assertions: `consumed == 0` after 200 fills; maker credit `== 200` despite `maxAssets == 100`. [5](#0-4) [7](#0-6) [8](#0-7)

### Citations

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

**File:** certora/specs/Consume.spec (L58-64)
```text
/// After a successful take, consumed[offer.maker][offer.group] does not exceed the effective max.
rule takeConsumedBoundedByMax(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert offer.maxAssets > 0 => consumed(offer.maker, offer.group) <= offer.maxAssets;
    assert offer.maxAssets == 0 => consumed(offer.maker, offer.group) <= offer.maxUnits;
}
```

**File:** src/libraries/TickLib.sol (L15-19)
```text

    /// @dev Returns x / d rounded to the nearest integer with ties rounded down, without checking for overflow.
    function divHalfDownUnchecked(uint256 x, uint256 d) internal pure returns (uint256) {
        unchecked {
            return (x + (d - 1) / 2) / d;
```

**File:** src/libraries/TickLib.sol (L44-51)
```text
    function tickToPrice(uint256 tick) internal pure returns (uint256) {
        require(tick <= MAX_TICK, TickOutOfRange());
        unchecked {
            // forge-lint: disable-next-item(unsafe-typecast)
            return uint256(1e36)
                    .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
                    .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
        }
```

**File:** certora/specs/NoMultiplicationOverflow.spec (L52-57)
```text
// Sound: tickToPrice = 1e36 / (1e18 + wExp(...)) and wExp(x) >= 0, so result <= WAD.
function boundedTickPrice() returns uint256 {
    uint256 price;
    require price <= WAD(), "Proven in TickToPrice.spec";
    return price;
}
```

**File:** test/TickLibTest.sol (L15-20)
```text
    function testTickToPriceMinMax() public pure {
        assertEq(TickLib.tickToPrice(0), 0, "tick 0");
        assertEq(TickLib.tickToPrice(2), 1e12, "first non-zero tick");
        assertEq(TickLib.tickToPrice(MAX_TICK - 2), 1e18 - 1e12, "tick max - 2 just below par");
        assertEq(TickLib.tickToPrice(MAX_TICK), 1e18, "tick max");
    }
```
