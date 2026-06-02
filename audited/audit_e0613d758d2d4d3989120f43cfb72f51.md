Audit Report

## Title
Unbounded `offer.maxAssets` causes arithmetic overflow in `mulDivUp`/`mulDivDown`, DoS-ing all bundler calls that include the offer - (`File: src/periphery/ConsumableUnitsLib.sol`)

## Summary
`ConsumableUnitsLib.consumableUnits` passes `offer.maxAssets.zeroFloorSub(consumed)` directly into `TakeAmountsLib.buyerAssetsToUnits` or `sellerAssetsToUnits`, both of which call `UtilsLib.mulDivUp`/`mulDivDown` using plain Solidity 0.8 checked multiplication. Because `offer.maxAssets` is an unbounded `uint256` with no magnitude check, any offer with `maxAssets > type(uint256).max / WAD` causes an arithmetic overflow revert. Since all four bundler functions in `MidnightBundles.sol` call `consumableUnits` outside their `try/catch` blocks, the entire bundler transaction reverts.

## Finding Description

**Root cause — `UtilsLib.mulDivUp` and `mulDivDown` use plain checked multiplication:** [1](#0-0) 

Both `(x * y) / d` and `(x * y + (d - 1)) / d` overflow when `x * y > type(uint256).max`. Solidity 0.8 checked arithmetic makes this a hard revert.

**`offer.maxAssets` is an unbounded `uint256`:** [2](#0-1) 

No magnitude bound is enforced anywhere in the offer struct or in `take()`.

**`consumableUnits` passes the raw remaining capacity directly into the arithmetic functions:** [3](#0-2) 

When `offer.maxUnits == 0` and `offer.buy == true`, it calls `buyerAssetsToUnits(..., offer.maxAssets.zeroFloorSub(consumed))`. When `offer.buy == false`, it calls `sellerAssetsToUnits` with the same value.

**`buyerAssetsToUnits` calls `mulDivUp(targetBuyerAssets, WAD, buyerPrice)`:** [4](#0-3) 

The `require(buyerPrice <= WAD)` guard only bounds the denominator `d`, not the multiplicand `x`. If `targetBuyerAssets > type(uint256).max / WAD` (≈ 1.157×10⁵⁹), then `x * WAD` overflows regardless of `buyerPrice`.

**`sellerAssetsToUnits` has the same issue via `mulDivDown`:** [5](#0-4) 

**The `consumableUnits` call is outside the `try/catch` in all four bundler functions:** [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) 

The `try/catch` wraps only `IMidnight(MIDNIGHT).take(...)`. The `consumableUnits` computation is evaluated before the `try` block, so its revert propagates to the caller unconditionally. The NatSpec on each function explicitly documents this: "Reverts if ConsumableUnitsLib reverts." [10](#0-9) [11](#0-10) 

**Exploit flow:**
1. Attacker (unprivileged maker) creates an offer with `offer.buy = true`, `offer.maxUnits = 0`, `offer.maxAssets = type(uint256).max`, any valid ratifier.
2. `atMostOneNonZero(maxAssets, maxUnits)` passes because `maxUnits == 0`.
3. Victim taker calls any bundler function with this offer in the `takes[]` array.
4. Bundler evaluates `ConsumableUnitsLib.consumableUnits` → `buyerAssetsToUnits(..., type(uint256).max)` → `mulDivUp(type(uint256).max, 1e18, buyerPrice)` → `type(uint256).max * 1e18` overflows → revert.
5. Entire bundler transaction reverts. The same applies to sell offers via `sellerAssetsToUnits` / `mulDivDown`.

## Impact Explanation
All four bundler entry points (`supplyCollateralAndSellWithUnitsTarget`, `buyWithUnitsTargetAndWithdrawCollateral`, `buyWithAssetsTargetAndWithdrawCollateral`, `supplyCollateralAndSellWithAssetsTarget`) are permanently DoS-able for any taker who includes a poisoned offer in their `takes[]` array. A malicious maker can publish such an offer to grief any bundler-based taker attempting to fill it, with no ongoing cost after offer creation. This constitutes a targeted, repeatable denial-of-service against the bundler periphery.

## Likelihood Explanation
Creating an offer requires no special privilege. Setting `maxAssets = type(uint256).max` is a valid `uint256` value accepted by `take()`. The overflow threshold (`> type(uint256).max / WAD ≈ 1.157×10⁵⁹`) is far below `type(uint256).max` (≈ 1.157×10⁷⁷), so any value above ~1.16×10⁵⁹ triggers it. The attack is repeatable and free to sustain.

## Recommendation
Cap `offer.maxAssets` at a safe upper bound (e.g., `type(uint128).max`) either in `Midnight.take()` validation or at the entry of `consumableUnits`/`buyerAssetsToUnits`/`sellerAssetsToUnits`. Alternatively, wrap the `consumableUnits` call inside the bundler's `try/catch` block so that a reverting offer is skipped rather than propagating to the caller. A third option is to use a `mulDiv` implementation that uses `unchecked` blocks or 512-bit intermediate arithmetic (e.g., Solmate/OpenZeppelin `FullMath`) to avoid overflow.

## Proof of Concept
```solidity
// Minimal forge test sketch
function test_consumableUnitsOverflow() public {
    // Attacker creates a buy offer with maxAssets = type(uint256).max, maxUnits = 0
    Offer memory poisonOffer = Offer({
        ...,
        buy: true,
        maxUnits: 0,
        maxAssets: type(uint256).max,
        ...
    });

    Take[] memory takes = new Take[](1);
    takes[0] = Take({ offer: poisonOffer, units: 1, ratifierData: "" });

    // Any bundler call including this offer reverts with arithmetic overflow
    vm.expectRevert(); // arithmetic overflow
    bundles.supplyCollateralAndSellWithUnitsTarget(
        1, 0, taker, receiver, new CollateralSupply[](0), takes, 0, address(0)
    );
}
```
The revert occurs at `ConsumableUnitsLib.consumableUnits` → `TakeAmountsLib.buyerAssetsToUnits` → `UtilsLib.mulDivUp` before the `try/catch` is ever reached.

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

**File:** src/periphery/MidnightBundles.sol (L43-43)
```text
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

**File:** src/periphery/MidnightBundles.sol (L111-111)
```text
    /// @dev Reverts if ConsumableUnitsLib reverts.
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
