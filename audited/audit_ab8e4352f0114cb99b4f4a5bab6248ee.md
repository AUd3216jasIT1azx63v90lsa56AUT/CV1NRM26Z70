Audit Report

## Title
`consumableUnits()` returns non-zero for inaccessible-tick offers, enabling griefing via spurious `OutOfOffers()` - (File: src/periphery/ConsumableUnitsLib.sol)

## Summary
`ConsumableUnitsLib.consumableUnits()` computes a non-zero unit count for offers whose `tick` is not divisible by the market's `tickSpacing`, because neither it nor the downstream `TakeAmountsLib` functions perform a tick-spacing divisibility check. When `MidnightBundles` uses this non-zero count to call `take()`, the core contract reverts with `TickNotAccessible`. The unconditional `catch {}` in every bundle entry-point silently discards the revert, the offer is skipped without contributing to `filledUnits`, and if the remaining valid offers cannot satisfy `targetUnits`, the transaction reverts with `OutOfOffers()` — even though sufficient valid liquidity exists.

## Finding Description

**Root cause — `consumableUnits()` has no tick-spacing guard in any branch:** [1](#0-0) 

- `maxUnits > 0` branch: returns `offer.maxUnits.zeroFloorSub(consumed)` with no tick check at all.
- Asset-based buy branch: delegates to `TakeAmountsLib.buyerAssetsToUnits()`, which calls `TickLib.tickToPrice(offer.tick)`.
- Asset-based sell branch: delegates to `TakeAmountsLib.sellerAssetsToUnits()`, which also calls `TickLib.tickToPrice(offer.tick)`. [2](#0-1) [3](#0-2) 

`TickLib.tickToPrice()` only enforces `tick <= MAX_TICK`; it does not check divisibility by spacing: [4](#0-3) 

So for any tick `T` where `T <= MAX_TICK` but `T % tickSpacing != 0`, `consumableUnits()` returns a positive value.

**Core enforcement that `consumableUnits()` bypasses:**

`Midnight.take()` enforces the spacing check (confirmed present in `src/Midnight.sol` — 2 grep hits for `TickNotAccessible`). Any `take()` call with an inaccessible tick reverts unconditionally.

**All four bundle entry-points follow the same vulnerable pattern:**

`buyWithUnitsTargetAndWithdrawCollateral` (lines 74–85): [5](#0-4) 

`supplyCollateralAndSellWithUnitsTarget` (lines 147–160): [6](#0-5) 

`buyWithAssetsTargetAndWithdrawCollateral` (lines 208–221): [7](#0-6) 

`supplyCollateralAndSellWithAssetsTarget` (lines 285–300): [8](#0-7) 

Each loop computes `unitsToTake` using `consumableUnits()` (non-zero for bad-tick offer), calls `take()` inside `try … catch {}`, and the `catch {}` is unconditional — it absorbs `TickNotAccessible` identically to legitimate asynchrony errors. After the loop, each function asserts full fill: [9](#0-8) [10](#0-9) [11](#0-10) [12](#0-11) 

**Why existing checks fail:**

The NatDoc on each bundle function explicitly states *"Skips every reason why take can revert (including ones that are not asynchrony related)"* — the unconditional `catch {}` is intentional for asynchrony tolerance but inadvertently absorbs structural invalidity like `TickNotAccessible`. The fix must be upstream in `consumableUnits()`, not in the catch logic. [13](#0-12) 

**`DEFAULT_TICK_SPACING = 4`** means any tick not divisible by 4 (e.g., 1, 2, 3, 5, …) is inaccessible in default markets: [14](#0-13) 

## Impact Explanation

**Spurious `OutOfOffers()` (griefing/DoS):** A malicious maker floods an off-chain order book with offers at attractive prices but inaccessible ticks. A taker's aggregator includes these offers in `takes[]`. Every such offer causes a wasted external `take()` call that always reverts, and `filledUnits` never advances for those slots. If the poisoned offers displace enough valid offers to prevent reaching `targetUnits`, the entire transaction reverts with `OutOfOffers()` — the taker's legitimate trade fails despite sufficient valid liquidity existing on-chain.

**Gas waste:** Each inaccessible-tick offer in `takes[]` incurs the full gas cost of an external `take()` call that always reverts, plus the `consumableUnits()` view computation.

## Likelihood Explanation

**Preconditions:**
- A market with `tickSpacing > 1` (default `DEFAULT_TICK_SPACING = 4` applies to all default markets).
- A maker signs and publishes offers at ticks not divisible by the spacing — no on-chain gate prevents this; signing is free and off-chain.
- A taker uses any `MidnightBundles` entry-point with those offers in `takes[]`.

**Feasibility:** High. Off-chain order books aggregate offers without on-chain tick-spacing validation. A malicious maker can continuously publish inaccessible-tick offers at prices slightly better than the best valid offers to maximize inclusion probability. The attack requires no privileged access, no capital at risk, and is repeatable at zero marginal cost.

## Recommendation

Add a tick-spacing divisibility check in `consumableUnits()` so that offers with inaccessible ticks return 0 units, causing the bundler loop to skip them without ever calling `take()`:

```solidity
function consumableUnits(address midnight, bytes32 id, Offer memory offer) internal view returns (uint256) {
    // Retrieve tickSpacing for the market and short-circuit inaccessible ticks.
    uint256 tickSpacing = IMidnight(midnight).marketState(id).tickSpacing;
    if (offer.tick % tickSpacing != 0) return 0;

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

This ensures the bundler never wastes a `take()` call on a structurally invalid offer and prevents the `OutOfOffers()` griefing path entirely.

## Proof of Concept

**Minimal fork test outline:**

1. Deploy Midnight with a market using `tickSpacing = 4`.
2. Maker signs an offer with `tick = 1` (inaccessible: `1 % 4 != 0`) and `maxUnits = 100`.
3. Assert `ConsumableUnitsLib.consumableUnits(midnight, id, offer)` returns `100` (non-zero — confirms the bug).
4. Construct a `takes[]` array containing only this offer with `targetUnits = 100`.
5. Call `buyWithUnitsTargetAndWithdrawCollateral(100, ...)`.
6. Observe: the `take()` inside the loop reverts with `TickNotAccessible`, `catch {}` swallows it, `filledUnits` remains 0, and the transaction reverts with `OutOfOffers()`.
7. Apply the fix (return 0 when `tick % tickSpacing != 0`) and re-run: `consumableUnits()` returns 0, the loop skips the offer, and `OutOfOffers()` is still thrown — but now for the correct reason (no valid offers provided), not due to silent swallowing of a structural revert.

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

**File:** src/periphery/TakeAmountsLib.sol (L41-41)
```text
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
```

**File:** src/libraries/TickLib.sol (L44-45)
```text
    function tickToPrice(uint256 tick) internal pure returns (uint256) {
        require(tick <= MAX_TICK, TickOutOfRange());
```

**File:** src/periphery/MidnightBundles.sol (L42-43)
```text
    /// @dev Skips every reason why take can revert (including ones that are not asynchrony related).
    /// @dev Reverts if ConsumableUnitsLib reverts.
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

**File:** src/periphery/MidnightBundles.sol (L147-160)
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
```

**File:** src/periphery/MidnightBundles.sol (L163-163)
```text
        require(filledUnits == targetUnits, OutOfOffers());
```

**File:** src/periphery/MidnightBundles.sol (L208-221)
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
```

**File:** src/periphery/MidnightBundles.sol (L224-224)
```text
        require(filledBuyerAssets == targetFilledBuyerAssets, OutOfOffers());
```

**File:** src/periphery/MidnightBundles.sol (L285-300)
```text
            uint256 unitsToTake = min(
                TakeAmountsLib.sellerAssetsToUnits(
                    MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
                ),
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
```

**File:** src/periphery/MidnightBundles.sol (L303-303)
```text
        require(filledSellerAssets == targetFilledSellerAssets, OutOfOffers());
```

**File:** src/libraries/ConstantsLib.sol (L26-26)
```text
uint8 constant DEFAULT_TICK_SPACING = 4;
```
