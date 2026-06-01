### Title
Stale `consumed` Read in `ConsumableUnitsLib.consumableUnits` Causes Silent Offer Skip and `OutOfOffers` Revert in Bundler - (`src/periphery/ConsumableUnitsLib.sol`)

### Summary
`ConsumableUnitsLib.consumableUnits` reads `consumed` at view time and computes `unitsToTake` calibrated to exactly exhaust the remaining asset capacity of a buy offer. If any other transaction fills any portion of that offer between the view call and the bundler's `take` call, the on-chain `consumed` value is higher than the stale snapshot, causing `Midnight.take` to revert with `ConsumedAssets`. The bundler's `catch {}` silently discards this revert, skips the offer, and if no further offers remain, reverts with `OutOfOffers`, blocking the user from lending.

### Finding Description

**Code path:**

`ConsumableUnitsLib.consumableUnits` (`src/periphery/ConsumableUnitsLib.sol`, line 15) reads the current on-chain `consumed` value:

```solidity
uint256 consumed = IMidnight(midnight).consumed(offer.maker, offer.group);
```

For a buy offer with `maxAssets > 0` (line 18–19), it computes:

```solidity
return TakeAmountsLib.buyerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
```

`buyerAssetsToUnits` (`src/periphery/TakeAmountsLib.sol`, line 29) returns:

```solidity
return offer.buy ? targetBuyerAssets.mulDivUp(WAD, buyerPrice) : ...
```

i.e., `units = ceil((maxAssets - consumed_view) * WAD / buyerPrice)`.

`Midnight.take` (`src/Midnight.sol`, lines 363, 368–369) then computes:

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : ...
newConsumed = consumed[offer.maker][offer.group] += buyerAssets;
require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

By design (confirmed by `testBuyerAssetsToUnitsBuyerIsLender`), `buyerAssets = maxAssets - consumed_view` exactly. So at take time, if another transaction has increased `consumed` by any `delta > 0`:

```
newConsumed = consumed_actual + buyerAssets
            = (consumed_view + delta) + (maxAssets - consumed_view)
            = maxAssets + delta  >  maxAssets  →  ConsumedAssets revert
```

In `supplyCollateralAndSellWithUnitsTarget` (`src/periphery/MidnightBundles.sol`, lines 152–160), this revert is caught silently:

```solidity
try IMidnight(MIDNIGHT).take(...) returns (...) {
    filledUnits += unitsToTake;
    ...
} catch {}
```

The offer is skipped. If no further offers cover the remaining `targetUnits`, line 163 reverts:

```solidity
require(filledUnits == targetUnits, OutOfOffers());
```

**Root cause:** `consumableUnits` computes a `unitsToTake` value that is valid only if `consumed` does not change between the view and the execute. There is no safety margin, no retry, and no distinction in the `catch {}` between "offer genuinely exhausted" and "offer partially front-run."

**Attacker inputs:** Any unprivileged taker calls `Midnight.take` directly on the same buy offer with any `units > 0` between the victim's off-chain simulation and the bundler's on-chain execution. No special permissions required.

### Impact Explanation
The user's `supplyCollateralAndSellWithUnitsTarget` call reverts with `OutOfOffers` even though the targeted buy offer still has remaining capacity. The user's collateral has already been supplied (lines 134–140 execute before the take loop), but no lending occurs. The user cannot lend as intended and must retry, potentially losing gas and facing repeated front-running.

### Likelihood Explanation
**Preconditions:** A buy offer with `maxAssets > 0` that is partially (but not fully) consumed. This is a normal, expected state for any active offer.

**Feasibility:** Any concurrent taker filling the same offer — even non-maliciously — triggers this. On a busy market with multiple takers competing for the same offer, this is a routine occurrence, not an edge case. A malicious actor can deliberately front-run with `units = 1` (minimum fill) to reliably trigger the revert at negligible cost.

**Repeatability:** The attack can be repeated on every retry attempt by the victim, permanently blocking them from using the bundler for that offer.

### Recommendation
In `ConsumableUnitsLib.consumableUnits`, for the assets-based buy offer branch, subtract a conservative rounding buffer from `targetBuyerAssets` before passing it to `buyerAssetsToUnits`, so the computed `unitsToTake` leaves at least 1 asset unit of headroom:

```solidity
} else if (offer.buy) {
    uint256 remaining = offer.maxAssets.zeroFloorSub(consumed);
    if (remaining == 0) return 0;
    return TakeAmountsLib.buyerAssetsToUnits(midnight, id, offer, remaining - 1);
```

Alternatively, the bundler's `catch {}` should re-read `consumed` after a `ConsumedAssets` failure and recompute `unitsToTake` with the fresh value rather than skipping the offer entirely. A more robust fix is to pass `unitsToTake - 1` (or use a partial-fill fallback) so that the computed units never exactly exhaust the stale remaining capacity.

### Proof of Concept

```solidity
// Foundry stateful test
function testFrontRunConsumedAssetsCausesOutOfOffers() public {
    // Setup: buy offer with maxAssets = 1000e18, consumed = 500e18 (partially filled)
    Offer memory buyOffer = ...; // offer.buy = true, offer.maxAssets = 1000e18
    vm.prank(maker);
    midnight.setConsumed(buyOffer.group, 500e18, maker); // consumed = 500e18

    // Step 1: off-chain simulation — consumableUnits returns X units for 500e18 remaining
    uint256 unitsToTake = ConsumableUnitsLib.consumableUnits(address(midnight), id, buyOffer);
    // unitsToTake is calibrated so buyerAssets = 500e18 exactly

    // Step 2: front-runner fills 1 asset unit of the same offer
    vm.prank(frontRunner);
    midnight.take(buyOffer, ..., 1, frontRunner, ...); // consumed becomes 500e18 + 1

    // Step 3: bundler executes with stale unitsToTake
    // take() computes buyerAssets = 500e18, newConsumed = 500e18+1+500e18 = 1000e18+1 > 1000e18
    // → ConsumedAssets revert → caught by catch {} → offer skipped
    vm.expectRevert(IMidnight.OutOfOffers.selector);
    bundler.supplyCollateralAndSellWithUnitsTarget(
        targetUnits, minSellerAssets, taker, receiver,
        collateralSupplies, takes, 0, address(0)
    );

    // Assert: consumed is still 500e18 + 1 (victim's take never executed)
    assertEq(midnight.consumed(maker, buyOffer.group), 500e18 + 1);
}
```

**Expected assertions:**
- `OutOfOffers` revert on the bundler call
- `consumed` reflects only the front-runner's fill, not the victim's intended fill
- Re-running `consumableUnits` after the revert returns a smaller (but still valid) value, confirming the offer was not exhausted [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** src/periphery/MidnightBundles.sol (L144-163)
```text
        for (uint256 i; i < takes.length && filledUnits < targetUnits; i++) {
            require(takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
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
