### Title
Rounding-to-zero in `mulDivDown` causes `consumed` to never increment for buy offers with `buyerPrice < WAD`, making `maxAssets` cap permanently unenforceable - (`src/Midnight.sol`)

### Summary
When a buy offer has `maxAssets > 0` and `buyerPrice < WAD` (i.e., `offerPrice < 1e18`, which is true for every tick below `MAX_TICK`), filling with `units = 1` computes `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`. The `consumed` mapping is incremented by zero on every such fill, so `newConsumed` never grows and the `require(newConsumed <= offer.maxAssets)` guard is permanently satisfied. The maker's credit position grows by 1 unit per fill without bound, violating the `maxAssets` cap entirely.

### Finding Description
**Exact code path — `src/Midnight.sol` lines 358–369:**

```solidity
uint256 offerPrice = TickLib.tickToPrice(offer.tick);          // always ≤ WAD
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
uint256 buyerPrice  = sellerPrice + _settlementFee;            // == offerPrice for buy offers
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // (1 * buyerPrice) / WAD == 0 when buyerPrice < WAD
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    // buyerAssets == 0  →  consumed unchanged  →  check always passes
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

**Price range:** `TickLib.tickToPrice` returns values in `(0, WAD]`. At `tick = MAX_TICK/2 = 2910` the price is ≈ `WAD/2`. At any tick below `MAX_TICK` the price is strictly less than `WAD`. The protocol itself documents the consequence at line 94: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* — but the same arithmetic means consumed never reaches `maxAssets` in the first place.

**Attacker-controlled inputs:**
- `offer.buy = true`, `offer.maxAssets > 0`, `offer.tick` set to any value where `tickToPrice(tick) < WAD` (e.g., tick = 2910 → price ≈ `5e17`)
- `units = 1` per call

**Exploit flow:**
1. Maker posts a buy offer with `maxAssets = X` (e.g., `1000e18`) at tick 2910 (`buyerPrice ≈ 5e17 < WAD`).
2. Taker calls `take(offer, ..., units=1, ...)` N times.
3. Each call: `buyerAssets = mulDivDown(1, 5e17, 1e18) = 0`; `consumed[maker][group] += 0`; guard `0 <= X` always passes.
4. After N calls: `consumed[maker][group] == 0`; `maker.credit += N`; the cap `maxAssets = X` is never enforced.

**Why existing checks fail:** The sole enforcement mechanism is `require(newConsumed <= offer.maxAssets)`. When `buyerAssets = 0` the increment is a no-op, so the check is trivially satisfied on every iteration regardless of how many fills have occurred.

### Impact Explanation
The `maxAssets` cap on buy offers is completely bypassed whenever `buyerPrice < WAD` and fills are submitted one unit at a time. The maker accumulates unbounded credit — far beyond the `maxAssets` limit they set — without `consumed` ever reflecting the actual fill activity. Any maker relying on `maxAssets` to bound their lending exposure is silently overexposed. The invariant *"offers cannot be overfilled"* is broken.

### Likelihood Explanation
The precondition (`buyerPrice < WAD`) holds for every tick below `MAX_TICK = 5820`, which is the overwhelming majority of the valid tick range. No special permissions are required; any unprivileged taker can call `take` with `units = 1` repeatedly. The attack is cheap on L2s (gas per call is low), repeatable indefinitely, and requires no oracle manipulation or privileged access. The only cost to the taker is the debt they accumulate, which they control voluntarily.

### Recommendation
Track consumed in units when `buyerPrice < WAD` causes `buyerAssets` to round to zero, or enforce a minimum `buyerAssets > 0` when `maxAssets > 0`. The cleanest fix is to add a floor: if `offer.maxAssets > 0` and `buyerAssets == 0`, either revert or increment `consumed` by 1 (one unit) so that every non-trivial fill advances the counter. Alternatively, switch the consumed accounting to always track units when `buyerPrice < WAD`, and document the conversion clearly.

### Proof of Concept
```solidity
// Foundry unit test sketch
function testConsumedNeverIncrements() public {
    // tick 2910 → buyerPrice ≈ WAD/2
    uint256 tick = 2910;
    uint256 maxAssets = 1000e18;

    Offer memory offer = Offer({
        buy: true,
        maxAssets: maxAssets,
        maxUnits: 0,
        tick: tick,
        // ... other fields (valid ratifier, expiry, etc.)
    });

    // Fund taker with collateral for N units of debt
    uint256 N = 2000; // > maxAssets in units
    collateralize(market, taker, N);

    uint256 consumedBefore = midnight.consumed(maker, offer.group);

    for (uint256 i = 0; i < N; i++) {
        vm.prank(taker);
        midnight.take(offer, ratifierData, 1, taker, address(0), address(0), "");
    }

    // Assertions that expose the bug:
    assertEq(midnight.consumed(maker, offer.group), 0);          // consumed never moved
    assertGt(midnight.position(id, maker).credit, maxAssets);    // credit exceeds cap
    // consumed == 0 < maxAssets == 1000e18, yet maker has > 1000e18 credit
}
```

Expected: `consumed == 0` after all fills; `maker.credit == N > maxAssets`. A correct implementation would have `consumed >= maxAssets` after `maxAssets` worth of fills, causing subsequent `take` calls to revert with `ConsumedAssets`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** src/Midnight.sol (L93-94)
```text
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

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```
