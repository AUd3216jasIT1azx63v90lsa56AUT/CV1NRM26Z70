### Title
Zero-asset rounding in `maxAssets` mode silently skips `consumed` increment, enabling indefinite offer reuse post-maturity — (File: src/Midnight.sol)

### Summary

When `offer.maxAssets > 0` and `offer.buy == true`, the `consumed` mapping is incremented by `buyerAssets` rather than by `units`. Because `buyerAssets` is computed with `mulDivDown` (floor division), a taker can choose `units` small enough that `buyerAssets = 0`, causing `consumed` to never increment. Post-maturity, where `sellerDebtIncrease == 0` is enforced, this allows the offer to be taken indefinitely: each call reduces the buyer's debt and the seller's credit by `units` with zero token transfer and zero `consumed` growth, completely bypassing the `maxAssets` cap.

### Finding Description

**Root cause — `src/Midnight.sol` line 368:**

```solidity
// offer.buy == true branch
newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

`buyerAssets` is computed at line 363:

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
```

`mulDivDown` in `src/libraries/UtilsLib.sol` is plain integer division:

```solidity
function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y) / d;
}
```

With `WAD = 1e18` and `buyerPrice = offerPrice` (for buy offers, since `buyerPrice = sellerPrice + settlementFee = offerPrice`), any `units` satisfying `units * offerPrice < 1e18` yields `buyerAssets = 0`.

**Concrete numeric example:**

- `tick` near `MAX_TICK = 5820` → `offerPrice ≈ 1e12` (minimum non-zero price, rounded to `PRICE_ROUNDING_STEP = 1e12`)
- `units = 1`
- `buyerAssets = 1 * 1e12 / 1e18 = 0`
- `sellerAssets = 1 * sellerPrice / 1e18 = 0` (since `sellerPrice ≤ buyerPrice`)

**Post-maturity path:**

`timeToMaturity = zeroFloorSub(market.maturity, block.timestamp) = 0`, so `settlementFee(id, 0) = settlementFeeCbp0 * CBP`. Even at the maximum 0-day fee of `14e12`, choosing `offerPrice = 15e12` keeps `sellerPrice = 1e12 > 0` and still gives `buyerAssets = 0`.

The post-maturity guard at line 391:

```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

is satisfied as long as the taker (seller) holds credit ≥ `units`, making `sellerCreditDecrease = units` and `sellerDebtIncrease = 0`.

**State changes per iteration (units = 1, buyerAssets = 0, sellerAssets = 0):**

| Field | Change |
|---|---|
| `consumed[maker][group]` | +0 (no increment) |
| `buyerPos.debt` | −1 (buyer's debt reduced) |
| `sellerPos.credit` | −1 (taker's credit consumed) |
| `_marketState.totalUnits` | −1 |
| token transfer | 0 |

Because `consumed` never grows, `require(newConsumed <= offer.maxAssets)` always passes, and the loop repeats indefinitely.

**No existing check stops it:** There is no `require(units == 0 || buyerAssets > 0)` guard. The `SelfTake` check prevents maker == taker, but two addresses controlled by the same party suffice. The Certora `Consume.spec` uses `NONDET` summaries for both `mulDivDown` and `mulDivUp` (lines 11–12), so the formal verification does not model the rounding-to-zero case; additionally, the `takeConsumedDelta` rule (line 67–74) is scoped to `offer.maxAssets == 0` only, leaving the `maxAssets > 0` path unchecked.

### Impact Explanation

The offer's `maxAssets` fill cap is completely bypassed. An attacker controlling the taker address (acting as the buyer's ally, or as the same economic entity across two addresses) can call `take` an unbounded number of times post-maturity, each time reducing the buyer's (maker's) debt by `units` without any asset payment. The buyer's entire debt can be zeroed out without a single token being transferred into the protocol. This violates the core invariant that "every credit has matching debt or valid settled/loss state" and the offer-replay invariant that "offers cannot be replayed, overfilled, reused, or filled after cancel/deadline."

### Likelihood Explanation

**Preconditions:**
1. `offer.maxAssets > 0`, `offer.buy == true` — standard lender offer configuration.
2. `offer.tick` near `MAX_TICK` — any tick where `offerPrice < 1e18 / units`; with `units = 1` this is any tick giving price < `1e18`, i.e., essentially all ticks.
3. `block.timestamp > market.maturity` — market has expired.
4. Taker holds credit ≥ `units` in the market — achievable by the attacker pre-positioning.
5. Offer `expiry` has not passed — maker sets a long expiry.

All preconditions are attacker-controllable. The attack is repeatable in a single transaction via `multicall` or a loop, and requires no privileged access.

### Recommendation

Add a guard that rejects non-zero-unit takes that produce zero assets in `maxAssets` mode:

```solidity
if (offer.maxAssets > 0) {
    uint256 assetsDelta = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || assetsDelta > 0, ZeroAssetsTake());
    newConsumed = consumed[offer.maker][offer.group] += assetsDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

This ensures every non-trivial take advances the `consumed` counter, preserving the fill-cap invariant. Alternatively, enforce a minimum `units` value such that the resulting asset amount is always ≥ 1 (i.e., `require(units == 0 || units >= WAD / (offer.buy ? buyerPrice : sellerPrice) + 1)`), though the first approach is simpler and more robust.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

// Foundry unit test (extend BaseTest)
function testConsumedNotIncrementedOnZeroBuyerAssets() public {
    // Setup: market past maturity
    uint256 maturity = block.timestamp - 1;
    market.maturity = maturity;
    id = midnight.touchMarket(market);

    // Use a tick near MAX_TICK so offerPrice is tiny (e.g., 1e12)
    // such that 1 * offerPrice / 1e18 == 0
    uint256 lowTick = MAX_TICK; // or nearest valid tick spacing multiple
    lenderOffer.market = market;
    lenderOffer.buy = true;
    lenderOffer.maker = lender;
    lenderOffer.maxAssets = 1e18; // large cap, should be consumed
    lenderOffer.maxUnits = 0;
    lenderOffer.tick = lowTick - (lowTick % DEFAULT_TICK_SPACING);
    lenderOffer.expiry = block.timestamp + 365 days;
    lenderOffer.ratifier = address(dummyRatifier);
    lenderOffer.group = bytes32(uint256(1));

    // Give borrower credit in the market (so sellerDebtIncrease == 0 post-maturity)
    // e.g., by a prior take pre-maturity that gave borrower credit
    // [setup borrower credit >= 1 here]

    uint256 consumedBefore = midnight.consumed(lender, lenderOffer.group);

    // Take with units=1, expecting buyerAssets=0 due to rounding
    vm.prank(borrower);
    (uint256 buyerAssets, ) = midnight.take(lenderOffer, hex"", 1, borrower, borrower, address(0), hex"");

    assertEq(buyerAssets, 0, "buyerAssets should be 0 due to rounding");

    uint256 consumedAfter = midnight.consumed(lender, lenderOffer.group);

    // BUG: consumed did not increment despite units > 0
    assertGt(consumedAfter, consumedBefore, "consumed must increment for every non-zero-units take");

    // Demonstrate reuse: take again — should fail if consumed is properly tracked
    vm.prank(borrower);
    midnight.take(lenderOffer, hex"", 1, borrower, borrower, address(0), hex"");
    // If consumed never increments, this succeeds indefinitely — demonstrating offer reuse
}
```

**Expected assertion failure:** `assertGt(consumedAfter, consumedBefore)` fails because `consumed` stays at `consumedBefore`, confirming the bug. The second `take` call also succeeds, confirming indefinite reuse. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** src/Midnight.sol (L391-391)
```text
        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** src/libraries/TickLib.sol (L5-8)
```text
int256 constant LN_ONE_PLUS_DELTA = 0.004987541511039073e18; // floor(ln(1.005) * 1e18)
uint256 constant MAX_TICK = 5820;
// Minimum representable price increment in WAD (1e-6 WAD). Tick prices are rounded to multiples of this value.
uint256 constant PRICE_ROUNDING_STEP = 1e12;
```

**File:** certora/specs/Consume.spec (L11-12)
```text
    function UtilsLib.mulDivDown(uint256, uint256, uint256) internal returns (uint256) => NONDET;
    function UtilsLib.mulDivUp(uint256, uint256, uint256) internal returns (uint256) => NONDET;
```

**File:** certora/specs/Consume.spec (L66-75)
```text
/// After a successful take in units mode, the change in consumed equals the units taken.
rule takeConsumedDelta(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxAssets == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert consumed(offer.maker, offer.group) == consumedBefore + units;
}
```
