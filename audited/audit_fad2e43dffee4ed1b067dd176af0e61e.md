Looking at the actual code, I need to verify each claim in the report.

**Claim 1: `zeroFloorSub(maxAssets, maxAssets) = 0`**

From `UtilsLib.sol` line 24: `z := mul(gt(x, y), sub(x, y))`. When `x == y`, `gt(x,y) = 0`, so `z = 0`. ✓ [1](#0-0) 

**Claim 2: `buyerAssetsToUnits(..., 0) = 0`**

From `TakeAmountsLib.sol` line 29, for a buy offer: `mulDivUp(0, WAD, buyerPrice)` = `(0 * WAD + (buyerPrice-1)) / buyerPrice` = `(buyerPrice-1)/buyerPrice` = 0 for any `buyerPrice >= 1`. ✓ [2](#0-1) 

**Claim 3: No zero-unit guard in bundler loops**

All four bundler functions compute `unitsToTake = min(...)` and immediately call `try IMidnight(MIDNIGHT).take(...)` with no `if (unitsToTake == 0) continue;` guard. ✓ [3](#0-2) [4](#0-3) 

**Claim 4: `take(units=0)` succeeds when `consumed == maxAssets`**

With `units=0` and `offer.buy=true`: `buyerAssets = mulDivDown(0, buyerPrice, WAD) = 0`. Then `newConsumed = consumed += 0 = maxAssets`, and `require(maxAssets <= maxAssets)` passes. ✓ [5](#0-4) 

**Claim 5: Callback invoked with 0 assets**

The `buyerCallback` check at line 445 has no guard against `buyerAssets == 0`, so `onBuy` is called with `buyerAssets=0, units=0`. ✓ [6](#0-5) 

**Claim 6: `OutOfOffers` revert when all offers exhausted**

`filledUnits += 0` on every iteration; after exhausting the array, `require(filledUnits == targetUnits, OutOfOffers())` reverts. ✓ [7](#0-6) 

All technical claims are verified against the actual code. The exploit path is valid and reproducible by any unprivileged user via front-running.

---

Audit Report

## Title
Missing zero-unit skip guard in bundler loops causes unnecessary `take` calls and `OutOfOffers` revert for fully consumed buy offers in maxAssets mode - (File: src/periphery/MidnightBundles.sol)

## Summary
When a buy offer has `maxAssets > 0` and `consumed[maker][group] == maxAssets`, `ConsumableUnitsLib.consumableUnits` returns 0. The four bundler loops in `MidnightBundles` have no `if (unitsToTake == 0) continue;` guard, so they call `Midnight.take` with 0 units. Because `take(0)` is a no-op that passes the `ConsumedAssets` check even when fully consumed, `filledUnits` does not advance, wasting an external call per exhausted offer and — if all offers are exhausted — causing the bundler to revert with `OutOfOffers`. If the offer has a callback, it is also invoked with 0 assets.

## Finding Description

**Root cause — `ConsumableUnitsLib.consumableUnits` returns 0:**

`ConsumableUnitsLib.sol` line 19: `offer.maxAssets.zeroFloorSub(consumed)` uses `mul(gt(x,y), sub(x,y))`, which is 0 when `x == y`. This 0 is passed to `TakeAmountsLib.buyerAssetsToUnits`, which computes `mulDivUp(0, WAD, buyerPrice) = (buyerPrice-1)/buyerPrice = 0` for any `buyerPrice >= 1`.

**No guard in bundler loops:**

All four functions (`supplyCollateralAndSellWithUnitsTarget`, `buyWithUnitsTargetAndWithdrawCollateral`, `supplyCollateralAndSellWithAssetsTarget`, `buyWithAssetsTargetAndWithdrawCollateral`) compute `unitsToTake = min(..., consumableUnits(...))` and immediately call `try IMidnight(MIDNIGHT).take(..., unitsToTake, ...)` with no zero check.

**`take(0)` succeeds as a no-op:**

In `Midnight.sol` lines 363–369, with `units=0` and `offer.buy=true`: `buyerAssets = mulDivDown(0, buyerPrice, WAD) = 0`; `newConsumed = consumed += 0 = maxAssets`; `require(maxAssets <= maxAssets)` passes. The call succeeds, `filledUnits += 0`, and the loop advances to `i+1` without progress.

**Callback side-effect:**

If `offer.callback != address(0)`, `onBuy` is invoked at line 445 with `buyerAssets=0, units=0` — an unintended invocation of the maker's callback.

**Exploit flow:**
1. Attacker front-runs victim's bundler call with a single `take` that sets `consumed == maxAssets`.
2. Victim calls any of the four bundler functions with the now-exhausted offer in `takes[]`.
3. `consumableUnits` returns 0; `unitsToTake = 0`; `take(0)` succeeds as no-op.
4. `filledUnits` does not advance; loop exhausts the array.
5. `require(filledUnits == targetUnits, OutOfOffers())` reverts.

**Existing protections are insufficient:** The `try/catch` only catches reverts; it does not skip zero-unit calls. The loop condition `filledUnits < targetUnits` does not prevent iterating over exhausted offers.

## Impact Explanation
Any fully consumed buy offer (maxAssets mode) in the `takes` array causes one unnecessary cross-contract `take` call. If the offer has a callback, it is invoked with 0 assets — an unintended side-effect that may have consequences depending on the callback implementation. If all offers in the array are fully consumed, the bundler reverts with `OutOfOffers` after burning gas on every wasted call, causing the victim's transaction to fail entirely. This is a concrete, reproducible griefing/DoS impact on any user of the four bundler functions.

## Likelihood Explanation
Preconditions: a buy offer with `maxAssets > 0` must be fully consumed before the victim's bundler transaction lands. Any taker (including a griefing attacker) can achieve this with a single `take` call. The attack is cheap, requires no special privileges, is repeatable, and applies identically to all four bundler functions. Front-running is straightforward on any chain with a public mempool.

## Recommendation
Add a zero-unit skip guard before each `try` block in all four bundler loops:

```solidity
if (unitsToTake == 0) continue;
```

This skips fully consumed offers without an external call, preventing wasted gas, unintended callback invocations, and the `OutOfOffers` revert caused by exhausted offers.

## Proof of Concept
1. Deploy Midnight and MidnightBundles on a fork.
2. Create a buy offer with `maxAssets = 100e18`.
3. As attacker, call `Midnight.take` to fully consume the offer (`consumed == maxAssets`).
4. As victim, call `supplyCollateralAndSellWithUnitsTarget` with `targetUnits > 0` and `takes = [exhaustedOffer]`.
5. Observe: `consumableUnits` returns 0; `take(0)` is called and succeeds; `filledUnits` remains 0; transaction reverts with `OutOfOffers`.
6. If the offer has `callback != address(0)`, additionally observe the callback is invoked with `buyerAssets=0, units=0`.

### Citations

**File:** src/libraries/UtilsLib.sol (L22-26)
```text
    function zeroFloorSub(uint256 x, uint256 y) internal pure returns (uint256 z) {
        assembly {
            z := mul(gt(x, y), sub(x, y))
        }
    }
```

**File:** src/periphery/TakeAmountsLib.sol (L29-29)
```text
        return offer.buy ? targetBuyerAssets.mulDivUp(WAD, buyerPrice) : targetBuyerAssets.mulDivDown(WAD, buyerPrice);
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

**File:** src/Midnight.sol (L363-369)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L445-453)
```text
        if (buyerCallback != address(0)) {
            bytes memory buyerCallbackData = offer.buy ? offer.callbackData : takerCallbackData;
            require(
                IBuyCallback(buyerCallback)
                    .onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
                == CALLBACK_SUCCESS,
                WrongBuyCallbackReturnValue()
            );
        }
```
