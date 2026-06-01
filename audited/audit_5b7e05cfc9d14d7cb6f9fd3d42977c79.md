Now I have all the information needed. Let me trace the exact code path.

### Title
`consumableUnits()` returns non-zero for inaccessible-tick offers, causing bundler gas waste and spurious `OutOfOffers()` - (File: src/periphery/ConsumableUnitsLib.sol)

### Summary
`ConsumableUnitsLib.consumableUnits()` computes units via `TakeAmountsLib.buyerAssetsToUnits()` / `sellerAssetsToUnits()`, which call `TickLib.tickToPrice(offer.tick)`. Neither function checks `offer.tick % tickSpacing == 0`. When `MidnightBundles` uses the returned non-zero value to call `take()`, the core contract reverts with `TickNotAccessible` at line 351 of `Midnight.sol`. The bundler's bare `catch {}` silently swallows the revert, the offer is skipped, and if the skipped offer was needed to reach `targetUnits`, the transaction reverts with `OutOfOffers()`.

### Finding Description

**Exact code path:**

`consumableUnits()` has three branches, none of which check tick accessibility:

```
// offer.maxUnits > 0 branch — no tick check at all
return offer.maxUnits.zeroFloorSub(consumed);

// asset-based buy branch
return TakeAmountsLib.buyerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
//   └─ calls TickLib.tickToPrice(offer.tick)  ← only checks tick <= MAX_TICK

// asset-based sell branch
return TakeAmountsLib.sellerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
//   └─ calls TickLib.tickToPrice(offer.tick)  ← only checks tick <= MAX_TICK
``` [1](#0-0) [2](#0-1) [3](#0-2) 

`Midnight.take()` enforces the spacing check as its third validation:

```solidity
require(offer.tick % _marketState.tickSpacing == 0, TickNotAccessible());
``` [4](#0-3) 

All four bundle entry-points in `MidnightBundles` follow the same pattern: compute `unitsToTake` using `consumableUnits()`, then call `take()` inside `try … catch {}`:

```solidity
uint256 unitsToTake = min(
    targetUnits - filledUnits,
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)  // non-zero for bad tick
);
try IMidnight(MIDNIGHT).take(..., unitsToTake, ...) returns (...) {
    filledUnits += unitsToTake;
    ...
} catch {}   // TickNotAccessible silently swallowed
``` [5](#0-4) 

After the loop, the bundler asserts full fill:

```solidity
require(filledUnits == targetUnits, OutOfOffers());
``` [6](#0-5) 

The same pattern repeats in `supplyCollateralAndSellWithUnitsTarget` (lines 147–163), `buyWithAssetsTargetAndWithdrawCollateral` (lines 208–224), and `supplyCollateralAndSellWithAssetsTarget` (lines 285–303). [7](#0-6) [8](#0-7) 

**Attacker-controlled inputs:**

Midnight offers are off-chain signed structures passed as calldata to `take()`. There is no on-chain enforcement preventing a maker from signing an offer at any tick value. A malicious maker signs an offer with `offer.tick = T` where `T % tickSpacing != 0` (e.g., tick 2921 when `tickSpacing == 4`). This offer is published to an off-chain order book. A taker who includes it in their `takes[]` array triggers the path above.

**Why existing checks fail:**

- `tickToPrice()` only checks `tick <= MAX_TICK`, not divisibility by spacing. [3](#0-2) 
- `consumableUnits()` has no tick-spacing guard. [1](#0-0) 
- The bundler's `catch {}` is unconditional — it absorbs `TickNotAccessible` identically to transient asynchrony errors. [9](#0-8) 

### Impact Explanation

1. **Gas waste**: Every inaccessible-tick offer in the `takes[]` array causes a full external `take()` call that always reverts, burning gas for the failed call and the `consumableUnits()` view computation.
2. **Spurious `OutOfOffers()`**: If the taker's `takes[]` list contains enough inaccessible-tick offers that the remaining valid offers cannot fill `targetUnits`, the transaction reverts with `OutOfOffers()` even though sufficient liquidity exists at valid ticks. A malicious maker can deliberately pollute an order book with inaccessible-tick offers to cause this for any taker relying on that book.

### Likelihood Explanation

**Preconditions:**
- A market exists with `tickSpacing > 1` (default is `DEFAULT_TICK_SPACING = 4`). [10](#0-9) 
- A maker signs and publishes an offer at a tick not divisible by the spacing (trivially achievable; no on-chain gate prevents it).
- A taker uses `MidnightBundles` with that offer in their `takes[]` array.

**Feasibility**: High. Off-chain order books aggregate offers without on-chain validation. A malicious maker can flood a book with inaccessible-tick offers at attractive prices to lure takers. The attack is repeatable at zero cost (signing is free) and requires no privileged access.

### Recommendation

Add a tick-accessibility guard at the top of `consumableUnits()`:

```solidity
function consumableUnits(address midnight, bytes32 id, Offer memory offer) internal view returns (uint256) {
    (, , , , , , , , , , , , uint8 spacing) = IMidnight(midnight).marketState(id);
    if (offer.tick % spacing != 0) return 0;
    // ... existing logic
}
```

Alternatively, add the same guard inside `buyerAssetsToUnits()` and `sellerAssetsToUnits()` in `TakeAmountsLib`, or expose a `tickSpacing(id)` view (already present on `IMidnight`) and check it in `ConsumableUnitsLib` before any computation.

### Proof of Concept

```solidity
// Foundry unit test
function testConsumableUnitsNonZeroForInaccessibleTick() public {
    // Market with default tickSpacing = 4
    bytes32 id = midnight.touchMarket(market);
    assertEq(midnight.tickSpacing(id), 4);

    // Offer at tick 2921 (2921 % 4 == 1, inaccessible)
    Offer memory offer = _makeOffer(2921);
    offer.maxUnits = 1000;

    // consumableUnits returns non-zero — invariant violated
    uint256 units = ConsumableUnitsLib.consumableUnits(address(midnight), id, offer);
    assertGt(units, 0, "consumableUnits should be 0 for inaccessible tick");

    // take() with this offer always reverts
    vm.prank(borrower);
    vm.expectRevert(IMidnight.TickNotAccessible.selector);
    midnight.take(offer, hex"", units, borrower, borrower, address(0), hex"");
}

function testBundlerOutOfOffersFromInaccessibleTick() public {
    bytes32 id = midnight.touchMarket(market);
    uint256 targetUnits = 100;

    // Only offer in the list is at an inaccessible tick
    Offer memory badOffer = _makeOffer(2921); // 2921 % 4 != 0
    badOffer.maxUnits = type(uint256).max;
    deal(address(loanToken), lender, type(uint128).max);
    collateralize(market, borrower, targetUnits);

    Take[] memory takes = new Take[](1);
    takes[0] = Take({offer: badOffer, units: type(uint256).max, ratifierData: hex""});

    vm.prank(borrower);
    // Expect OutOfOffers because consumableUnits returns non-zero but take always reverts
    vm.expectRevert(IMidnightBundles.OutOfOffers.selector);
    midnightBundles.buyWithUnitsTargetAndWithdrawCollateral(
        targetUnits, type(uint256).max, borrower, _noPermit(), takes,
        new CollateralWithdrawal[](0), address(0), 0, address(0)
    );
}
```

**Expected assertions:**
- First test: `assertGt(units, 0)` passes (invariant violated), and `take()` reverts with `TickNotAccessible`.
- Second test: bundler reverts with `OutOfOffers()` despite the offer appearing consumable.

### Citations

**File:** src/periphery/ConsumableUnitsLib.sol (L14-23)
```text
    function consumableUnits(address midnight, bytes32 id, Offer memory offer) internal view returns (uint256) {
        uint256 consumed = IMidnight(midnight).consumed(offer.maker, offer.group);
        if (offer.maxUnits > 0) {
            return offer.maxUnits.zeroFloorSub(consumed);
        } else if (offer.buy) {
            return TakeAmountsLib.buyerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
        } else {
            return TakeAmountsLib.sellerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
        }
    }
```

**File:** src/periphery/TakeAmountsLib.sol (L22-22)
```text
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
```

**File:** src/libraries/TickLib.sol (L44-45)
```text
    function tickToPrice(uint256 tick) internal pure returns (uint256) {
        require(tick <= MAX_TICK, TickOutOfRange());
```

**File:** src/Midnight.sol (L96-99)
```text
/// TICK SPACING
/// @dev Offers can only be placed at ticks that are multiples of the market's spacing.
/// @dev Newly created markets start at the global DEFAULT_TICK_SPACING.
/// @dev The tickSpacingSetter can decrease the spacing to a divisor of the current spacing, unlocking new ticks only.
```

**File:** src/Midnight.sol (L351-351)
```text
        require(offer.tick % _marketState.tickSpacing == 0, TickNotAccessible());
```

**File:** src/periphery/MidnightBundles.sol (L74-85)
```text
            uint256 unitsToTake = min(
                targetUnits - filledUnits,
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
            try IMidnight(MIDNIGHT)
                .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (
                uint256 resBuyerAssets, uint256
            ) {
                filledUnits += unitsToTake;
                filledBuyerAssets += resBuyerAssets;
            } catch {}
```

**File:** src/periphery/MidnightBundles.sol (L88-88)
```text
        require(filledUnits == targetUnits, OutOfOffers());
```

**File:** src/periphery/MidnightBundles.sol (L147-163)
```text
            uint256 unitsToTake = min(
                targetUnits - filledUnits,
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
            try IMidnight(MIDNIGHT)
                .take(
                    takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(this), address(0), ""
                ) returns (
                uint256, uint256 resSellerAssets
            ) {
                filledUnits += unitsToTake;
                filledSellerAssets += resSellerAssets;
            } catch {}
        }

        require(filledUnits == targetUnits, OutOfOffers());
```

**File:** src/periphery/MidnightBundles.sol (L208-224)
```text
            uint256 unitsToTake = min(
                TakeAmountsLib.buyerAssetsToUnits(
                    MIDNIGHT, id, takes[i].offer, targetFilledBuyerAssets - filledBuyerAssets
                ),
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
            try IMidnight(MIDNIGHT)
                .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (
                uint256 resBuyerAssets, uint256
            ) {
                filledUnits += unitsToTake;
                filledBuyerAssets += resBuyerAssets;
            } catch {}
        }

        require(filledBuyerAssets == targetFilledBuyerAssets, OutOfOffers());
```
