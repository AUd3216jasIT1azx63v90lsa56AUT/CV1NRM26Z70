Audit Report

## Title
Unbounded `offer.maxAssets` causes arithmetic overflow in `mulDivUp`/`mulDivDown`, DoS-ing all bundler calls that include the offer - (`File: src/periphery/ConsumableUnitsLib.sol`)

## Summary
`ConsumableUnitsLib.consumableUnits` passes `offer.maxAssets.zeroFloorSub(consumed)` directly into `TakeAmountsLib.buyerAssetsToUnits` or `sellerAssetsToUnits`, both of which invoke `UtilsLib.mulDivUp`/`mulDivDown` using plain Solidity 0.8 checked multiplication. Because `offer.maxAssets` is an unbounded `uint256` with no magnitude check, any offer with `maxAssets > type(uint256).max / WAD` causes an arithmetic overflow revert. Since all four bundler functions in `MidnightBundles.sol` evaluate `consumableUnits` outside their `try/catch` blocks, the entire bundler transaction reverts unconditionally.

## Finding Description

**Root cause — `UtilsLib.mulDivUp` and `mulDivDown` use plain checked multiplication:**

Both `(x * y) / d` and `(x * y + (d - 1)) / d` overflow when `x * y > type(uint256).max`. Solidity 0.8 checked arithmetic makes this a hard revert with no recovery path. [1](#0-0) 

**`offer.maxAssets` is an unbounded `uint256`:**

The `Offer` struct declares `uint256 maxAssets` with no magnitude constraint. The only check in `take()` is `atMostOneNonZero(offer.maxAssets, offer.maxUnits)`, which passes when `maxUnits == 0` and `maxAssets == type(uint256).max`. No upper bound is enforced anywhere. [2](#0-1) [3](#0-2) 

**`consumableUnits` passes the raw remaining capacity directly into the arithmetic functions:**

When `offer.maxUnits == 0` and `offer.buy == true`, it calls `buyerAssetsToUnits(..., offer.maxAssets.zeroFloorSub(consumed))`. If `consumed == 0` (fresh offer), `targetBuyerAssets = type(uint256).max`. The same applies to sell offers via `sellerAssetsToUnits`. [4](#0-3) 

**`buyerAssetsToUnits` calls `mulDivUp(targetBuyerAssets, WAD, buyerPrice)`:**

The `require(buyerPrice <= WAD)` guard only bounds the denominator `d`, not the multiplicand `x`. With `targetBuyerAssets = type(uint256).max` and `WAD = 1e18`, the expression `type(uint256).max * 1e18` overflows regardless of `buyerPrice`. [5](#0-4) 

**`sellerAssetsToUnits` has the same issue via `mulDivDown`:** [6](#0-5) 

**The `consumableUnits` call is outside the `try/catch` in all four bundler functions:**

In every bundler loop, `ConsumableUnitsLib.consumableUnits(...)` is evaluated as an argument to `min(...)` assigned to `unitsToTake` before the `try IMidnight(MIDNIGHT).take(...)` block. A revert in `consumableUnits` propagates unconditionally to the caller. The NatSpec on each function explicitly documents: *"Reverts if ConsumableUnitsLib reverts."* [7](#0-6) [8](#0-7) [9](#0-8) [10](#0-9) 

**Exploit flow:**
1. Attacker (unprivileged maker) creates an offer with `offer.buy = true`, `offer.maxUnits = 0`, `offer.maxAssets = type(uint256).max`, any valid ratifier.
2. `atMostOneNonZero(type(uint256).max, 0)` returns `true` — passes.
3. Victim taker calls any bundler function with this offer in the `takes[]` array.
4. Bundler evaluates `consumableUnits` → `buyerAssetsToUnits(..., type(uint256).max)` → `mulDivUp(type(uint256).max, 1e18, buyerPrice)` → `type(uint256).max * 1e18` overflows → revert.
5. Entire bundler transaction reverts. The same applies to sell offers via `sellerAssetsToUnits` / `mulDivDown`.

## Impact Explanation
All four bundler entry points (`supplyCollateralAndSellWithUnitsTarget`, `buyWithUnitsTargetAndWithdrawCollateral`, `buyWithAssetsTargetAndWithdrawCollateral`, `supplyCollateralAndSellWithAssetsTarget`) are permanently DoS-able for any taker who includes a poisoned offer in their `takes[]` array. A malicious maker can publish such an offer to grief any bundler-based taker attempting to fill it, with no ongoing cost after offer creation. This constitutes a targeted, repeatable denial-of-service against the bundler periphery. [11](#0-10) [12](#0-11) 

## Likelihood Explanation
Creating an offer requires no special privilege — any address can be a maker. Setting `maxAssets = type(uint256).max` is a valid `uint256` value accepted by `take()`. The overflow threshold (`> type(uint256).max / WAD ≈ 1.157×10⁵⁹`) is far below `type(uint256).max` (≈ 1.157×10⁷⁷), so any value above ~1.16×10⁵⁹ triggers it. The attack is repeatable and free to sustain after offer creation. [13](#0-12) 

## Recommendation
Replace the plain checked multiplication in `UtilsLib.mulDivUp` and `mulDivDown` with an overflow-safe implementation using 512-bit intermediate arithmetic (e.g., Solmate's `FullMath.mulDiv` or a similar assembly-based approach). Alternatively, add a magnitude cap in `ConsumableUnitsLib.consumableUnits` before passing the remaining capacity to `buyerAssetsToUnits`/`sellerAssetsToUnits` — for example, capping at `type(uint128).max`, consistent with how the existing test suite bounds `maxAssets` in practice. A third option is to wrap the `consumableUnits` call itself in a `try/catch` within the bundler loop so that a poisoned offer is skipped rather than reverting the entire transaction. [14](#0-13) 

## Proof of Concept
Minimal Foundry unit test:

```solidity
// 1. Deploy Midnight and MidnightBundles (standard test setup).
// 2. Attacker creates a buy offer:
Offer memory poisoned = /* valid offer fields */;
poisoned.buy = true;
poisoned.maxUnits = 0;
poisoned.maxAssets = type(uint256).max;
// ratifier = any authorized ratifier

// 3. Victim taker calls bundler with poisoned offer in takes[]:
Take[] memory takes = new Take[](1);
takes[0] = Take({offer: poisoned, ratifierData: "", units: 1});

vm.expectRevert(); // arithmetic overflow
bundles.buyWithUnitsTargetAndWithdrawCollateral(
    1, type(uint256).max, taker, emptyPermit, takes, new CollateralWithdrawal[](0), taker, 0, address(0)
);
// Verify: same revert for all four bundler entry points.
```

The overflow is deterministic: `type(uint256).max * 1e18` always overflows in checked Solidity 0.8 arithmetic, so no fuzzing is required — a single concrete call suffices. [15](#0-14)

### Citations

**File:** src/libraries/UtilsLib.sol (L29-36)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }

    /// @dev Returns (x * y) / d rounded up.
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
```

**File:** src/interfaces/IMidnight.sol (L34-35)
```text
    uint256 maxUnits;
    uint256 maxAssets; // buyerAssets if offer.buy else sellerAssets
```

**File:** src/Midnight.sol (L346-356)
```text
        require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
        bytes32 id = touchMarket(offer.market);
        MarketState storage _marketState = marketState[id];
        require(_marketState.lossFactor < type(uint128).max, MarketLossFactorMaxedOut());
        require(UtilsLib.atMostOneNonZero(offer.maxAssets, offer.maxUnits), MultipleNonZero());
        require(offer.tick % _marketState.tickSpacing == 0, TickNotAccessible());
        require(block.timestamp >= offer.start, OfferNotStarted());
        require(block.timestamp <= offer.expiry, OfferExpired());
        require(offer.maker != taker, SelfTake());
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
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

**File:** src/periphery/TakeAmountsLib.sol (L28-29)
```text
        require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne());
        return offer.buy ? targetBuyerAssets.mulDivUp(WAD, buyerPrice) : targetBuyerAssets.mulDivDown(WAD, buyerPrice);
```

**File:** src/periphery/TakeAmountsLib.sol (L44-46)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        return
            offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

**File:** src/periphery/MidnightBundles.sol (L43-44)
```text
    /// @dev Reverts if ConsumableUnitsLib reverts.
    /// @dev If taking an offer reverts, the bundler will completely skip this offer.
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

**File:** src/periphery/MidnightBundles.sol (L111-112)
```text
    /// @dev Reverts if ConsumableUnitsLib reverts.
    /// @dev If taking an offer reverts, the bundler will completely skip this offer.
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
