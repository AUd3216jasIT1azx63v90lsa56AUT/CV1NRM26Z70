Audit Report

## Title
Stale `consumed` Read in `ConsumableUnitsLib.consumableUnits` Enables Front-Run DoS on Bundler Take Loops - (File: src/periphery/ConsumableUnitsLib.sol)

## Summary
`ConsumableUnitsLib.consumableUnits` reads `consumed` at view time and computes `unitsToTake` calibrated to exactly exhaust the remaining capacity of an offer. Any concurrent fill of that offer between the view and the on-chain `take` causes `Midnight.take` to revert with `ConsumedAssets` or `ConsumedUnits`. The bundler's `catch {}` silently discards this revert, and if no further offers cover the remaining `targetUnits`, the entire bundler transaction reverts with `OutOfOffers`. A malicious actor can front-run with `units = 1` at negligible cost to reliably trigger this on every retry.

## Finding Description

**Root cause — stale `consumed` read:**

`ConsumableUnitsLib.consumableUnits` reads the on-chain `consumed` value at view time:

```solidity
// src/periphery/ConsumableUnitsLib.sol:15
uint256 consumed = IMidnight(midnight).consumed(offer.maker, offer.group);
```

For `maxUnits > 0`, it returns `offer.maxUnits.zeroFloorSub(consumed)`. For `maxAssets > 0` with a buy offer, it returns `TakeAmountsLib.buyerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed))`.

**Why the arithmetic causes an exact-capacity take:**

For the `maxUnits` path, `unitsToTake = maxUnits - consumed_view`. In `Midnight.take`:

```solidity
// src/Midnight.sol:371-372
newConsumed = consumed[offer.maker][offer.group] += units;
require(newConsumed <= offer.maxUnits, ConsumedUnits());
```

Without a concurrent fill, `newConsumed = consumed_view + (maxUnits - consumed_view) = maxUnits` — exactly at the cap.

For the `maxAssets` path with a buy offer, `buyerAssetsToUnits` returns `ceil(targetBuyerAssets * WAD / buyerPrice)`. In `take`, `buyerAssets = floor(units * buyerPrice / WAD)`. Because `buyerPrice ≤ WAD` (enforced by `require(buyerPrice <= WAD, ...)`), the ceiling/floor round-trip is exact: `floor(ceil(x * WAD / p) * p / WAD) = x`. So `buyerAssets = maxAssets - consumed_view` exactly, and `newConsumed = maxAssets` — again exactly at the cap.

**Exploit flow:**

1. Victim submits a bundler call (e.g., `supplyCollateralAndSellWithUnitsTarget`) targeting an offer with remaining capacity `R`.
2. Bundler calls `consumableUnits` at view time, obtaining `unitsToTake = R` (or equivalent).
3. Attacker front-runs with `Midnight.take(offer, ..., 1, ...)` — `consumed` on-chain becomes `consumed_view + 1`.
4. Victim's `take` executes: `newConsumed = (consumed_view + 1) + R = maxUnits + 1 > maxUnits` → `ConsumedUnits` revert.
5. Bundler catches the revert silently:

```solidity
// src/periphery/MidnightBundles.sol:152-160
try IMidnight(MIDNIGHT).take(...) returns (...) {
    filledUnits += unitsToTake;
    ...
} catch {}
```

6. No further offers → `require(filledUnits == targetUnits, OutOfOffers())` reverts the entire transaction.
7. Attacker repeats on every retry.

**Existing guards are insufficient:**

The `catch {}` is intentional (documented at lines 42–44 of `MidnightBundles.sol`: "Skips every reason why take can revert") but makes no distinction between "offer genuinely exhausted" and "offer partially front-run." There is no safety margin, no partial-fill fallback, and no retry logic.

## Impact Explanation
The user's bundler call reverts with `OutOfOffers` even though the targeted offer still has remaining capacity. The user loses gas on every attempt. A malicious actor can permanently block the user from successfully executing any of the four bundler functions for a given offer by front-running each retry at negligible cost (a single `Midnight.take` with `units = 1`). No funds are permanently lost (the transaction is atomic), but the bundler entry point is rendered unusable for the targeted offer.

## Likelihood Explanation
**Preconditions:** Any active offer with `maxUnits > 0` or `maxAssets > 0` that is not yet fully consumed — a normal, expected state. **Feasibility:** Any unprivileged taker calling `Midnight.take` directly on the same offer triggers this, even non-maliciously in a competitive market. A malicious actor can deliberately front-run with `units = 1` at minimal gas cost. **Repeatability:** The attack can be repeated on every retry, permanently blocking the victim from using the bundler for that offer as long as the attacker continues front-running.

## Recommendation
Introduce a safety margin in `consumableUnits` so that `unitsToTake` is strictly less than the full remaining capacity (e.g., subtract 1 unit or a small percentage). Alternatively, after a `take` revert, re-read the current `consumed` value and retry with the updated remainder rather than skipping the offer entirely. A third option is to pass `unitsToTake` as a maximum rather than an exact amount, if the protocol supports partial fills below the computed value without reverting.

## Proof of Concept
**Minimal fork test plan:**
1. Deploy `Midnight` and `MidnightBundles`.
2. Create a buy offer with `maxUnits = 100`, `consumed = 0`.
3. In a single test transaction, simulate two calls:
   a. Attacker calls `Midnight.take(offer, ..., 1, taker=attacker, ...)` — sets `consumed = 1`.
   b. Victim calls `MidnightBundles.supplyCollateralAndSellWithUnitsTarget(targetUnits=100, ..., takes=[{offer, units=100}], ...)`.
4. Inside the bundler, `consumableUnits` reads `consumed = 1` (already updated by attacker), returns `99`. But if the attacker's call is ordered first in the same block, `consumableUnits` reads `consumed = 0` at simulation time and returns `100`, while at execution time `consumed = 1`, causing `newConsumed = 101 > 100` → revert.
5. Assert the bundler call reverts with `OutOfOffers`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** src/Midnight.sol (L366-373)
```text
        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
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

**File:** src/periphery/TakeAmountsLib.sol (L17-30)
```text
    function buyerAssetsToUnits(address midnight, bytes32 id, Offer memory offer, uint256 targetBuyerAssets)
        internal
        view
        returns (uint256)
    {
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
        uint256 settlementFee =
            IMidnight(midnight).settlementFee(id, UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp));
        // Mirrors Midnight's computation to revert if offerPrice < settlementFee in case of a buy offer.
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + settlementFee;
        require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne());
        return offer.buy ? targetBuyerAssets.mulDivUp(WAD, buyerPrice) : targetBuyerAssets.mulDivDown(WAD, buyerPrice);
    }
```
