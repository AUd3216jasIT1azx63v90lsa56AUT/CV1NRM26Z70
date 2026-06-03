After reading all relevant source files, I can confirm the claim is technically accurate. Here is the validated report:

---

Audit Report

## Title
Unbounded `offer.maxAssets` causes arithmetic overflow in `mulDivUp`/`mulDivDown`, DoS-ing all bundler calls that include the offer - (`File: src/periphery/ConsumableUnitsLib.sol`)

## Summary
`ConsumableUnitsLib.consumableUnits` passes `offer.maxAssets.zeroFloorSub(consumed)` directly into `TakeAmountsLib.buyerAssetsToUnits` or `sellerAssetsToUnits`, both of which call `UtilsLib.mulDivUp`/`mulDivDown` using plain Solidity 0.8 checked multiplication. Because `offer.maxAssets` is an unbounded `uint256` with no magnitude check, any offer with `maxAssets > type(uint256).max / WAD` causes an arithmetic overflow revert. Since all four bundler functions in `MidnightBundles.sol` evaluate `consumableUnits` outside their `try/catch` blocks, the entire bundler transaction reverts unconditionally.

## Finding Description

**`UtilsLib.mulDivUp` and `mulDivDown` use plain checked multiplication:**

`mulDivDown` computes `(x * y) / d` and `mulDivUp` computes `(x * y + (d - 1)) / d`. Both overflow when `x * y > type(uint256).max` under Solidity 0.8 checked arithmetic. [1](#0-0) 

**`offer.maxAssets` is an unbounded `uint256`:**

The only check in `take()` is `require(UtilsLib.atMostOneNonZero(offer.maxAssets, offer.maxUnits), MultipleNonZero())`, which merely ensures at most one of the two fields is nonzero. It imposes no upper bound on `maxAssets`. [2](#0-1) 

**`consumableUnits` passes the raw remaining capacity directly into the arithmetic functions:**

When `offer.maxUnits == 0` and `offer.buy == true`, it calls `buyerAssetsToUnits(..., offer.maxAssets.zeroFloorSub(consumed))`. When `offer.buy == false`, it calls `sellerAssetsToUnits` with the same value. If `consumed == 0` and `maxAssets == type(uint256).max`, the full `type(uint256).max` is forwarded. [3](#0-2) 

**`buyerAssetsToUnits` calls `mulDivUp(targetBuyerAssets, WAD, buyerPrice)`:**

The `require(buyerPrice <= WAD)` guard only bounds the denominator, not the multiplicand. If `targetBuyerAssets > type(uint256).max / WAD` (≈ 1.157×10⁵⁹), then `targetBuyerAssets * WAD` overflows regardless of `buyerPrice`. [4](#0-3) 

**`sellerAssetsToUnits` has the same issue via `mulDivUp`/`mulDivDown`:** [5](#0-4) 

**The `consumableUnits` call is outside the `try/catch` in all four bundler functions:**

In each bundler function, `ConsumableUnitsLib.consumableUnits(...)` is evaluated as an argument to `min(...)` when computing `unitsToTake`, which occurs before the `try IMidnight(MIDNIGHT).take(...)` block. A revert from `consumableUnits` therefore propagates unconditionally to the caller. The NatSpec on each function explicitly documents this: *"Reverts if ConsumableUnitsLib reverts."* [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) 

**Exploit flow:**
1. Attacker (unprivileged maker) creates an offer with `offer.buy = true`, `offer.maxUnits = 0`, `offer.maxAssets = type(uint256).max`, and any ratifier they control (authorized via `setIsAuthorized`).
2. `atMostOneNonZero(type(uint256).max, 0)` passes because `maxUnits == 0`.
3. Victim taker calls any bundler function with this offer in the `takes[]` array.
4. Bundler evaluates `ConsumableUnitsLib.consumableUnits` → `buyerAssetsToUnits(..., type(uint256).max)` → `mulDivUp(type(uint256).max, WAD, buyerPrice)` → `type(uint256).max * WAD` overflows → revert.
5. Entire bundler transaction reverts. The same applies to sell offers via `sellerAssetsToUnits` / `mulDivDown`.

## Impact Explanation
All four bundler entry points (`supplyCollateralAndSellWithUnitsTarget`, `buyWithUnitsTargetAndWithdrawCollateral`, `buyWithAssetsTargetAndWithdrawCollateral`, `supplyCollateralAndSellWithAssetsTarget`) are permanently DoS-able for any taker who includes a poisoned offer in their `takes[]` array. A malicious maker can publish such an offer to grief any bundler-based taker attempting to fill it, with no ongoing cost after offer creation. This constitutes a targeted, repeatable denial-of-service against the bundler periphery. No funds are lost (transactions revert), but the bundler is rendered unusable for any `takes[]` array containing the poisoned offer.

## Likelihood Explanation
Creating an offer requires no special privilege. Setting `maxAssets = type(uint256).max` is a valid `uint256` value accepted by `take()`. The overflow threshold (`> type(uint256).max / WAD ≈ 1.157×10⁵⁹`) is far below `type(uint256).max` (≈ 1.157×10⁷⁷), so any value above ~1.16×10⁵⁹ triggers it. The attack is repeatable and free to sustain. An attacker can post an offer with an attractive price to lure takers into including it in their `takes[]` array.

## Recommendation
Cap `offer.maxAssets` to a safe maximum before passing it into `buyerAssetsToUnits`/`sellerAssetsToUnits`. Specifically, in `ConsumableUnitsLib.consumableUnits`, clamp the remaining capacity:

```solidity
uint256 remaining = offer.maxAssets.zeroFloorSub(consumed);
uint256 safeRemaining = remaining > type(uint256).max / WAD ? type(uint256).max / WAD : remaining;
```

Alternatively, use a `mulDiv` implementation that avoids intermediate overflow (e.g., via 512-bit multiplication), or add a magnitude check on `offer.maxAssets` in `take()` (e.g., `require(offer.maxAssets <= type(uint128).max)`). The test suite already uses `type(uint128).max` as the practical maximum for `maxAssets`. [10](#0-9) 

## Proof of Concept
```solidity
// Minimal Foundry test
function testConsumableUnitsOverflow() public {
    // Attacker creates a buy offer with maxAssets = type(uint256).max
    Offer memory poisonedOffer = lenderOffer;
    poisonedOffer.maxUnits = 0;
    poisonedOffer.maxAssets = type(uint256).max;

    bytes32 id = midnight.touchMarket(poisonedOffer.market);

    // consumableUnits reverts with arithmetic overflow
    vm.expectRevert(); // arithmetic overflow
    ConsumableUnitsLib.consumableUnits(address(midnight), id, poisonedOffer);
}
```

Any bundler call with this offer in `takes[]` will revert unconditionally before reaching the `try/catch` block, as `consumableUnits` is evaluated as a function argument in the `min(...)` call. [11](#0-10)

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

**File:** src/Midnight.sol (L350-350)
```text
        require(UtilsLib.atMostOneNonZero(offer.maxAssets, offer.maxUnits), MultipleNonZero());
```

**File:** src/periphery/ConsumableUnitsLib.sol (L16-22)
```text
        if (offer.maxUnits > 0) {
            return offer.maxUnits.zeroFloorSub(consumed);
        } else if (offer.buy) {
            return TakeAmountsLib.buyerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
        } else {
            return TakeAmountsLib.sellerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
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
