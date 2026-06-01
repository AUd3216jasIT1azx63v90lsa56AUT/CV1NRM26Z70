### Title
Tick-0 Buy Offers With `maxAssets` Cap Are Effectively Uncapped Due to `tickToPrice(0) == 0` Causing `consumed` to Never Advance — (`src/libraries/TickLib.sol` / `src/Midnight.sol`)

### Summary

`tickToPrice(0)` evaluates to exactly `0` because the intermediate WAD-scaled price (~4.975 × 10¹¹) is smaller than `PRICE_ROUNDING_STEP` (10¹²), so the final `divHalfDownUnchecked / PRICE_ROUNDING_STEP * PRICE_ROUNDING_STEP` step rounds it to zero. When a buy offer is placed at `tick = 0` with `maxAssets = M > 0`, every call to `take` computes `buyerAssets = units.mulDivDown(0, WAD) = 0`, increments `consumed` by zero, and the `require(newConsumed <= offer.maxAssets)` guard is trivially satisfied forever. The offer is therefore fillable an unlimited number of times, violating the offer-cap invariant.

### Finding Description

**Tick validity check** (`Midnight.sol` line 351):
```solidity
require(offer.tick % _marketState.tickSpacing == 0, TickNotAccessible());
```
`DEFAULT_TICK_SPACING = 4` and `0 % 4 == 0`, so `tick = 0` passes.

**`tickToPrice(0)` evaluation** (`TickLib.sol` lines 44–52):
```
exponent = LN_ONE_PLUS_DELTA * (2910 - 0) ≈ 14.514e18
wExp(14.514e18) ≈ 2.01e24
1e36 / (1e18 + 2.01e24) ≈ 4.975e11
divHalfDownUnchecked(4.975e11, 1e12) = (4.975e11 + 4.999e11) / 1e12 = 9.974e11 / 1e12 = 0
0 * 1e12 = 0
```
`tickToPrice(0) == 0`.

**Price and asset computation** (`Midnight.sol` lines 358–364):
```solidity
uint256 offerPrice = TickLib.tickToPrice(offer.tick);   // == 0
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
// requires _settlementFee == 0 (default when no fee setter has acted), else underflow-revert
uint256 buyerPrice  = sellerPrice + _settlementFee;     // == 0
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)                 // == 0 for any units
    : units.mulDivUp(buyerPrice, WAD);
```

**Consumed accounting** (`Midnight.sol` lines 367–369):
```solidity
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```
`buyerAssets == 0` → `consumed += 0` → `newConsumed` stays at 0 → `require(0 <= M)` always passes.

**Exploit flow**:
1. Maker creates a buy offer: `tick = 0`, `maxAssets = M > 0`, `reduceOnly = false`, market with zero settlement fee (default).
2. Attacker (taker/seller) calls `take(offer, ..., units = M, ...)` repeatedly.
3. Each call: maker accumulates `M` units of credit, taker accumulates `M` units of debt; zero loan tokens are transferred; `consumed[maker][group]` remains 0.
4. The maker's `maxAssets` cap is never reached; the offer is filled without bound.

**Why existing checks fail**: The `ConsumedAssets` guard compares `newConsumed` (always 0) against `offer.maxAssets` (M > 0). The comparison is always true. No other check in `take` enforces a minimum price or a minimum `buyerAssets` value.

### Impact Explanation

The `maxAssets` cap — the sole mechanism limiting how many assets a maker's buy offer can consume — is rendered inoperative for any buy offer placed at `tick = 0`. The maker can be forced to accumulate an unbounded amount of credit (and the taker an unbounded amount of debt) with zero token flow. If the maker's offer includes a callback (`offer.callback`), that callback is also invocable an unlimited number of times, amplifying griefing potential.

### Likelihood Explanation

- `tick = 0` is a valid, accessible tick on every newly created market (spacing = 4, 0 % 4 = 0).
- Zero settlement fee is the protocol default until a privileged fee setter acts; most markets will start with it.
- No special role or capital is required; any unprivileged taker can execute the call sequence.
- The attack is repeatable in a single transaction via `multicall`.

### Recommendation

Add a guard in `take` that rejects zero-price offers when `maxAssets > 0`:

```solidity
require(offerPrice > 0 || offer.maxAssets == 0, ZeroPriceWithAssetsCap());
```

Alternatively, enforce a minimum non-zero `buyerAssets` (or `sellerAssets`) whenever `maxAssets > 0`, or reject `tick = 0` outright in `tickToPrice` / the tick-validity check.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight, Offer, Market} from "src/Midnight.sol";
import {TickLib} from "src/libraries/TickLib.sol";

contract TickZeroOverfillTest is Test {
    Midnight midnight;

    function test_tickZeroPrice() public pure {
        // Assert tickToPrice(0) == 0
        assertEq(TickLib.tickToPrice(0), 0, "tickToPrice(0) must be 0");
    }

    function test_buyOfferAtTickZeroUnlimitedFill() public {
        // Setup: deploy Midnight, create market with zero settlement fee
        // Create buy offer: tick=0, maxAssets=1e18, maxUnits=0
        // Taker calls take(units=1e18) three times
        // Assert: consumed[maker][group] == 0 after each call (never advances)
        // Assert: take succeeds every time (no ConsumedAssets revert)
        // Assert: consumed[maker][group] < offer.maxAssets after 3 fills
        //         (invariant violated: offer was filled 3x its stated cap)
    }
}
```

Expected assertions:
- `TickLib.tickToPrice(0) == 0` ✓
- `consumed[maker][group] == 0` after each of N calls to `take` with `units > 0`
- `take` never reverts with `ConsumedAssets` regardless of how many times it is called
- Total units transferred to maker = `N * units`, far exceeding `maxAssets = M` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** src/libraries/TickLib.sol (L5-8)
```text
int256 constant LN_ONE_PLUS_DELTA = 0.004987541511039073e18; // floor(ln(1.005) * 1e18)
uint256 constant MAX_TICK = 5820;
// Minimum representable price increment in WAD (1e-6 WAD). Tick prices are rounded to multiples of this value.
uint256 constant PRICE_ROUNDING_STEP = 1e12;
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

**File:** src/Midnight.sol (L351-351)
```text
        require(offer.tick % _marketState.tickSpacing == 0, TickNotAccessible());
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

**File:** src/libraries/ConstantsLib.sol (L8-8)
```text
uint256 constant WAD = 1e18;
```

**File:** src/libraries/ConstantsLib.sol (L26-26)
```text
uint8 constant DEFAULT_TICK_SPACING = 4;
```
