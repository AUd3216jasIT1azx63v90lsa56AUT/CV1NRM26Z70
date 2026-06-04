### Title
Division by Zero in `sellerAssetsToUnits` When `sellerPrice == 0` Causes Bundler DoS — (File: src/periphery/TakeAmountsLib.sol)

### Summary

`TakeAmountsLib.sellerAssetsToUnits` computes `sellerPrice = offerPrice - settlementFee` for buy offers. When `offerPrice == settlementFee`, `sellerPrice` becomes 0 and is used as the denominator in `mulDivUp(WAD, sellerPrice)`, causing an arithmetic revert. This propagates out of the `try/catch` boundary in `MidnightBundles.supplyCollateralAndSellWithAssetsTarget`, reverting the entire bundler transaction even though the core `take()` would accept such an offer without reverting.

### Finding Description

In `TakeAmountsLib.sellerAssetsToUnits`:

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
``` [1](#0-0) 

When `offer.buy == true` and `offerPrice == settlementFee`, `sellerPrice = 0`. The call `targetSellerAssets.mulDivUp(WAD, 0)` expands to `(targetSellerAssets * WAD + (0 - 1)) / 0`, where `0 - 1` underflows on `uint256`, triggering an arithmetic revert in Solidity 0.8. [2](#0-1) 

By contrast, the core `Midnight.take()` computes `sellerAssets = units.mulDivDown(0, WAD) = 0` and does **not** revert — it simply delivers 0 assets to the seller. [3](#0-2) 

The bundler calls `sellerAssetsToUnits` (and `ConsumableUnitsLib.consumableUnits`, which also calls it) **outside** the `try/catch` block:

```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(...),   // <-- NOT inside try/catch
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(...)    // <-- also NOT inside try/catch
);
try IMidnight(MIDNIGHT).take(...) returns (...) { ... } catch {}
``` [4](#0-3) 

So a revert in `sellerAssetsToUnits` propagates to the caller and aborts the entire `supplyCollateralAndSellWithAssetsTarget` transaction.

The function's own NatSpec only documents the `offerPrice < settlementFee` revert case, not the `offerPrice == settlementFee` case:

> `@dev Reverts if offerPrice < settlementFee in case of a buy offer (midnight reverts too).` [5](#0-4) 

The `offerPrice == settlementFee` case is silently missing from both the guard and the documentation.

### Impact Explanation

Any call to `supplyCollateralAndSellWithAssetsTarget` that includes a buy offer where `tickToPrice(offer.tick) == settlementFee` will revert unconditionally. The user's collateral has already been supplied to Midnight at that point (lines 134–139 execute before the loop), so the revert wastes gas and leaves the user's collateral deposited without the intended borrow being executed. The core protocol is unaffected; only the periphery bundler is broken for this edge case. [6](#0-5) 

### Likelihood Explanation

`offerPrice` is a multiple of `PRICE_ROUNDING_STEP = 1e12` and `settlementFee` is a multiple of `CBP = 1e12`, so exact equality is arithmetically possible. The settlement fee at 360 days reaches up to 50 bps (5e15), and `tickToPrice` at low ticks produces prices in the 1e12–1e15 range, making the equality reachable with real market parameters. A maker can deliberately post a buy offer at the matching tick; if an off-chain routing system includes it in a user's `takes` array, the bundler call reverts. [7](#0-6) 

### Recommendation

Add a guard in `sellerAssetsToUnits` for the `sellerPrice == 0` case, consistent with how `buyerAssetsToUnits` guards against `buyerPrice > WAD`:

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
require(sellerPrice > 0, "sellerPrice is zero");
```

Alternatively, in `supplyCollateralAndSellWithAssetsTarget`, wrap the `unitsToTake` computation in a `try/catch` and skip offers that cause a revert, consistent with the existing `try/catch` around `take()`.

### Proof of Concept

1. Deploy Midnight with a market whose settlement fee at TTM = 0 is `F` (e.g., `F = 1e12`).
2. Find tick `t` such that `tickToPrice(t) == F` (possible since both are multiples of 1e12).
3. Maker posts a buy offer at tick `t` with `offer.buy = true`.
4. User calls `supplyCollateralAndSellWithAssetsTarget` with `takes = [offer_at_tick_t]`.
5. Inside the loop, `TakeAmountsLib.sellerAssetsToUnits(... offer ...)` computes `sellerPrice = F - F = 0` and calls `mulDivUp(WAD, 0)`, which reverts with arithmetic underflow.
6. The entire transaction reverts, even though `Midnight.take()` would have accepted the offer (returning `sellerAssets = 0`). [8](#0-7) [9](#0-8)

### Citations

**File:** src/periphery/TakeAmountsLib.sol (L33-35)
```text
    /// @dev Assumes that id and offer.market match.
    /// @dev Reverts if offerPrice < settlementFee in case of a buy offer (midnight reverts too).
    /// @dev Returns a number of units for which take yields exactly targetSellerAssets (not necessarily the smallest).
```

**File:** src/periphery/TakeAmountsLib.sol (L36-47)
```text
    function sellerAssetsToUnits(address midnight, bytes32 id, Offer memory offer, uint256 targetSellerAssets)
        internal
        view
        returns (uint256)
    {
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
        uint256 settlementFee =
            IMidnight(midnight).settlementFee(id, UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp));
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        return
            offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
    }
```

**File:** src/libraries/UtilsLib.sol (L34-36)
```text
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
```

**File:** src/Midnight.sol (L361-364)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/periphery/MidnightBundles.sol (L134-163)
```text
        for (uint256 i; i < collateralSupplies.length; i++) {
            address token = market.collateralParams[collateralSupplies[i].collateralIndex].token;
            pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
            forceApproveMax(token, MIDNIGHT);
            IMidnight(MIDNIGHT)
                .supplyCollateral(market, collateralSupplies[i].collateralIndex, collateralSupplies[i].assets, taker);
        }

        uint256 filledUnits;
        uint256 filledSellerAssets;
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

**File:** src/libraries/TickLib.sol (L6-8)
```text
uint256 constant MAX_TICK = 5820;
// Minimum representable price increment in WAD (1e-6 WAD). Tick prices are rounded to multiples of this value.
uint256 constant PRICE_ROUNDING_STEP = 1e12;
```

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
