Audit Report

## Title
Division-by-Zero in `sellerAssetsToUnits` When `tickToPrice(tick) == settlementFee` Causes DoS of `supplyCollateralAndSellWithAssetsTarget` - (File: src/periphery/TakeAmountsLib.sol)

## Summary

In `TakeAmountsLib.sellerAssetsToUnits`, when `offer.buy == true` and `tickToPrice(offer.tick) == settlementFee`, `sellerPrice` is computed as exactly `0`. The subsequent call to `mulDivUp(targetSellerAssets, WAD, 0)` reverts due to Solidity 0.8 checked-arithmetic underflow at `(d - 1)` with `d = 0`. Because `Midnight.take()` does not revert in this same state (it simply yields `sellerAssets = 0`), the NatDoc invariant "midnight reverts too" is false for the equality case. Since `supplyCollateralAndSellWithAssetsTarget` calls `sellerAssetsToUnits` outside its `try/catch` block, any `takes[]` array containing such an offer causes the entire bundler call to revert unconditionally.

## Finding Description

**Root cause — `sellerAssetsToUnits` (TakeAmountsLib.sol lines 44–46):**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...
```

When `offer.buy == true` and `offerPrice == settlementFee`, `sellerPrice = 0`. [1](#0-0) 

`mulDivUp` in `UtilsLib.sol` is implemented as:

```solidity
function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y + (d - 1)) / d;
}
```

With `d = 0`, the expression `(d - 1)` underflows under Solidity 0.8 checked arithmetic, causing an unconditional revert. [2](#0-1) 

**Why `take()` does NOT revert in the same state (Midnight.sol lines 361–364):**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;  // = 0
uint256 sellerAssets = units.mulDivDown(sellerPrice, WAD);  // = (units * 0) / WAD = 0
```

`mulDivDown` divides by `WAD` (1e18), not by `sellerPrice`. No revert occurs; `sellerAssets` is simply `0`. [3](#0-2) 

This directly contradicts the NatDoc comment at line 34 of `TakeAmountsLib.sol`:

> "Reverts if offerPrice < settlementFee in case of a buy offer (midnight reverts too)"

The comment is incorrect for the equality case (`offerPrice == settlementFee`): `midnight` does not revert, but `sellerAssetsToUnits` does. [4](#0-3) 

**Exploit path in `supplyCollateralAndSellWithAssetsTarget` (MidnightBundles.sol lines 285–300):**

```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(   // ← OUTSIDE try/catch; reverts here
        MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
    ),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
);
try IMidnight(MIDNIGHT).take(...) returns (...) { ... } catch {}  // never reached
```

The `sellerAssetsToUnits` call is not wrapped in the `try/catch`. Its revert propagates unconditionally to the caller. The NatDoc for this function explicitly acknowledges this: "Reverts if TakeAmountsLib or ConsumableUnitsLib reverts." [5](#0-4) [6](#0-5) 

**Structural reachability of `offerPrice == settlementFee`:**

Both tick prices and settlement fees are rounded to multiples of `PRICE_ROUNDING_STEP = 1e12` (as confirmed in `TickLib.tickToPrice` and test setup). Settlement fees are public view functions. An attacker reads the current settlement fee, calls `TickLib.priceToTick(settlementFee, tickSpacing)` to find the matching tick, and posts a buy offer at that tick. No privileged access is required. [7](#0-6) [8](#0-7) 

## Impact Explanation

Any call to `supplyCollateralAndSellWithAssetsTarget` that includes a buy offer at the settlement-fee price point in its `takes[]` array reverts entirely. An aggregator or UI that enumerates open buy offers and routes through the malicious offer will have the victim's entire bundler transaction reverted. This blocks the sell-via-periphery path for affected takers for as long as the offer exists and the settlement fee remains at a level with a matching tick.

## Likelihood Explanation

- Settlement fees are public and readable on-chain; the attacker trivially computes the matching tick using `TickLib.priceToTick`.
- Both tick prices and settlement fees are multiples of `1e12`, making exact equality structurally guaranteed to be reachable for any non-zero settlement fee within the tick range.
- The attacker bears only the gas cost of posting one buy offer; no capital is at risk.
- The DoS persists until the offer is cancelled (by the attacker, who has no incentive to do so) or the settlement fee changes to a value with no matching tick.
- The attack is repeatable: if the fee changes, the attacker posts a new offer at the new matching tick.

## Recommendation

Add a guard in `sellerAssetsToUnits` for the `sellerPrice == 0` case. Since taking such an offer yields `sellerAssets = 0` regardless of units, the function should return `type(uint256).max` (or a sentinel indicating "no finite unit count achieves a positive seller asset target"), consistent with how `buyerAssetsToUnits` handles the analogous `buyerPrice > WAD` case via `require`. Alternatively, wrap the `sellerAssetsToUnits` call in `supplyCollateralAndSellWithAssetsTarget` in a `try/catch` (or an inline check) so that a revert from the library skips the offer rather than propagating to the caller.

Concretely, in `TakeAmountsLib.sellerAssetsToUnits`:

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
require(!offer.buy || sellerPrice > 0, SellerPriceIsZero());
```

## Proof of Concept

1. Deploy `Midnight` and `MidnightBundles` on a fork or test environment.
2. Set the market settlement fee to any value `F` that is a multiple of `1e12` and within the allowed range (e.g., `F = 1e12`).
3. Compute `tick = TickLib.priceToTick(F, tickSpacing)` such that `tickToPrice(tick) == F`.
4. As the attacker (unprivileged maker), post a buy offer at `tick` with `offer.buy = true`.
5. As the victim, call `supplyCollateralAndSellWithAssetsTarget` with `takes[]` containing the attacker's offer.
6. Observe that the call reverts at `sellerAssetsToUnits` due to `mulDivUp(..., 0)` underflow, before the `try/catch` is ever reached.
7. Confirm that calling `Midnight.take()` directly on the same offer with any `units > 0` succeeds and returns `sellerAssets = 0` without reverting.

### Citations

**File:** src/periphery/TakeAmountsLib.sol (L34-34)
```text
    /// @dev Reverts if offerPrice < settlementFee in case of a buy offer (midnight reverts too).
```

**File:** src/periphery/TakeAmountsLib.sol (L44-46)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        return
            offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
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

**File:** src/periphery/MidnightBundles.sol (L175-176)
```text
    /// @dev Reverts if TakeAmountsLib or ConsumableUnitsLib reverts.
    /// @dev If taking an offer reverts, the bundler will completely skip this offer.
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

**File:** src/libraries/TickLib.sol (L8-8)
```text
uint256 constant PRICE_ROUNDING_STEP = 1e12;
```

**File:** src/libraries/TickLib.sol (L44-52)
```text
    function tickToPrice(uint256 tick) internal pure returns (uint256) {
        require(tick <= MAX_TICK, TickOutOfRange());
        unchecked {
            // forge-lint: disable-next-item(unsafe-typecast)
            return uint256(1e36)
                    .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
                    .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
        }
    }
```
