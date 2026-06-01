### Title
`mulDivUp` rounding on per-fill `consumed` increment for sell offers with `maxAssets` exhausts offer capacity prematurely - (`src/Midnight.sol`)

### Summary
For sell offers (`offer.buy=false`) with `maxAssets > 0`, the `consumed` counter is incremented by `units.mulDivUp(sellerPrice, WAD)` per fill. When a taker submits `units=1` repeatedly, `mulDivUp(1, sellerPrice, WAD)` evaluates to exactly `1` for any `sellerPrice < WAD`, regardless of the actual fractional price. This causes `consumed` to reach `maxAssets=M` after only `M` fills (M total units), while the maker intended to fill `floor(M * WAD / sellerPrice)` units — a number strictly greater than `M` whenever `sellerPrice < WAD`.

### Finding Description

**Exact code path — `src/Midnight.sol` lines 364 and 368–369:**

```solidity
// line 364
uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

// lines 367–369
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

**`mulDivUp` identity for `units=1`:**

From `src/libraries/UtilsLib.sol` line 34–36:
```solidity
function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y + (d - 1)) / d;
}
```

`mulDivUp(1, sellerPrice, WAD) = (sellerPrice + WAD - 1) / WAD`.

For any `0 < sellerPrice < WAD`, this equals `1` (integer division). `tickToPrice` in `src/libraries/TickLib.sol` maps tick 0 → near-zero price and tick MAX_TICK (5820) → near-WAD price, with all intermediate prices strictly below WAD. So for every tick except the degenerate `sellerPrice = WAD` edge, each fill with `units=1` adds exactly `1` to `consumed`.

**Exploit flow:**

Preconditions: `offer.buy=false`, `offer.maxAssets=M`, `offer.tick=T` with `sellerPrice = tickToPrice(T) < WAD`.

1. Taker calls `take(offer, ..., units=1, ...)` — `sellerAssets = mulDivUp(1, sellerPrice, WAD) = 1`; `consumed += 1`.
2. Taker repeats M times.
3. After M calls: `consumed = M = maxAssets`; `require(newConsumed <= offer.maxAssets)` passes on the M-th call and reverts on any subsequent call.
4. Offer is fully exhausted. Total units filled = M.

**Intended capacity:** `floor(M * WAD / sellerPrice)` units. For `sellerPrice = 0.5 WAD`, this is `2M`. The taker has consumed the offer after `M` units — half the intended capacity.

**Why existing checks fail:** The `require(newConsumed <= offer.maxAssets)` guard (line 369) is the only protection. It correctly enforces the asset cap but does not prevent the rounding-amplified per-fill increment. There is no minimum-units check anywhere in `take()`.

### Impact Explanation

The maker's sell offer is exhausted after `M` units instead of the intended `floor(M * WAD / sellerPrice)` units. For a tick near the midpoint (sellerPrice ≈ 0.5 WAD), the maker loses half their intended borrowing capacity. For low-price ticks (sellerPrice ≈ 0.001 WAD), the maker loses ~99.9% of intended capacity. The maker receives M assets total (correct), but at an effective interest rate far above the tick price — they borrow far fewer units than intended for the same asset outlay.

### Likelihood Explanation

Preconditions are minimal: any sell offer with `maxAssets > 0` and any tick below MAX_TICK is vulnerable. The taker need only be an unprivileged address with enough loan tokens to pay 1 asset per fill. The attack is repeatable across any number of fills up to `maxAssets`. The cost to the attacker is real (they overpay per unit), making this a griefing vector rather than a profit-making exploit, but it is fully reachable with no special permissions.

### Recommendation

Replace the per-fill `mulDivUp` increment with `mulDivDown` when updating `consumed` for sell offers, so the counter tracks the floor of assets received rather than the ceiling:

```solidity
// Instead of sellerAssets (which uses mulDivUp), use a floor-rounded value for consumed:
uint256 consumedDelta = offer.buy ? buyerAssets : units.mulDivDown(sellerPrice, WAD);
newConsumed = consumed[offer.maker][offer.group] += consumedDelta;
require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

Alternatively, enforce a minimum `units` threshold (e.g., `require(units >= WAD / sellerPrice)`) to prevent sub-unit fills from triggering the rounding amplification. A third option is to track `consumed` in units (not assets) when `maxAssets` is set, converting the cap at check time: `require(units_total * sellerPrice / WAD <= maxAssets)`.

### Proof of Concept

```solidity
// Foundry fuzz test
function testConsumedRoundingGriefSellOffer(uint256 tick, uint256 M) public {
    tick = bound(tick, 1, MAX_TICK - 1); // sellerPrice < WAD
    M = bound(M, 10, 1000);

    uint256 sellerPrice = TickLib.tickToPrice(tick);
    uint256 intendedUnits = M * WAD / sellerPrice; // floor(M * WAD / sellerPrice)

    // Setup: maker creates sell offer with maxAssets = M
    offer.buy = false;
    offer.maxAssets = M;
    offer.maxUnits = 0;
    offer.tick = tick;

    collateralize(market, maker, intendedUnits + 1);
    deal(loanToken, taker, M + 1);

    // Attacker fills M times with units=1
    uint256 totalUnitsFilled;
    for (uint256 i = 0; i < M; i++) {
        take(1, taker, offer);
        totalUnitsFilled += 1;
    }

    // Offer is now exhausted (consumed == M)
    assertEq(midnight.consumed(offer.maker, offer.group), M);

    // Assert: total units filled is far below intended capacity
    // Invariant: totalUnitsFilled >= intendedUnits - M (per-fill rounding slack)
    // This assertion FAILS: totalUnitsFilled == M << intendedUnits
    assertGe(totalUnitsFilled, intendedUnits - M,
        "offer exhausted before intended units filled");
}
```

Expected: the assertion fails for any tick where `sellerPrice < WAD / 2` (i.e., `intendedUnits > 2M`), demonstrating that the offer is exhausted after `M` units instead of `floor(M * WAD / sellerPrice)` units. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** src/Midnight.sol (L364-369)
```text
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/libraries/UtilsLib.sol (L33-36)
```text
    /// @dev Returns (x * y) / d rounded up.
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
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
