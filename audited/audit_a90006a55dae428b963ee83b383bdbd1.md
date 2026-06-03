Audit Report

## Title
`sellerAssetsToUnits` reverts on division-by-zero when `offerPrice == settlementFee`, DoS-ing `supplyCollateralAndSellWithAssetsTarget` - (File: src/periphery/TakeAmountsLib.sol)

## Summary
`TakeAmountsLib.sellerAssetsToUnits` computes `sellerPrice = offerPrice - settlementFee` and passes it as the divisor to `mulDivUp`. When `offerPrice == settlementFee`, `sellerPrice = 0`, and `mulDivUp`'s expression `(d - 1)` underflows unconditionally in Solidity 0.8+ checked arithmetic. Because `supplyCollateralAndSellWithAssetsTarget` calls `sellerAssetsToUnits` outside its `try/catch` block, the revert propagates to the caller, permanently DoS-ing that entry-point for any bundle whose `takes[]` array includes a buy offer at the settlement-fee price tick.

## Finding Description

**Root cause — `mulDivUp` with `d = 0`:**

`UtilsLib.mulDivUp` is implemented as:
```solidity
function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y + (d - 1)) / d;
}
``` [1](#0-0) 

There is no `unchecked` block. With `d = 0`, the sub-expression `(d - 1)` is `(0 - 1)`, which underflows unconditionally in Solidity 0.8+ checked arithmetic, causing a revert before the division executes.

**How `sellerPrice` reaches zero:**

`sellerAssetsToUnits` computes:
```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...
``` [2](#0-1) 

When `offer.buy == true` and `offerPrice == settlementFee`, `sellerPrice = 0`, and `mulDivUp(targetSellerAssets, WAD, 0)` triggers the underflow. The NatSpec on this function documents revert only for `offerPrice < settlementFee`; the `==` case is undocumented and unguarded. [3](#0-2) 

**Core `take()` does NOT revert in the same scenario:**

In `Midnight.sol`, the equivalent computation uses `mulDivDown(units, sellerPrice, WAD)`:
```solidity
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
``` [4](#0-3) 

With `sellerPrice = 0`, `mulDivDown` computes `(units * 0) / WAD = 0`. No underflow occurs; `take()` succeeds and the seller receives zero assets. This asymmetry means the offer is valid and accepted by the core, but the periphery helper reverts trying to compute how many units to take.

**`supplyCollateralAndSellWithAssetsTarget` calls `sellerAssetsToUnits` outside the `try/catch`:**

```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(   // <-- outside try/catch
        MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
    ),
    ...
);
try IMidnight(MIDNIGHT).take(...) returns (...) { ... } catch {}
``` [5](#0-4) 

The `try/catch` guards only `IMidnight.take()`. A revert from `sellerAssetsToUnits` is not caught and bubbles up, reverting the entire bundle transaction. The NatSpec on the function explicitly acknowledges this: [6](#0-5) 

## Impact Explanation
Any call to `supplyCollateralAndSellWithAssetsTarget` whose `takes[]` array contains a buy offer at the settlement-fee price tick reverts entirely, including all collateral supply operations already executed in the same call (though those are also reverted since the whole transaction fails). The attacker can grief any taker who includes that offer, permanently blocking the periphery sell path for that bundle configuration. Because the offer itself is valid and `take()` would accept it, the DoS is invisible to the taker until the bundle reverts.

## Likelihood Explanation
Settlement fees are discrete governance-set values; tick prices are also discrete. The condition `tickToPrice(T) == settlementFee` is achievable whenever the fee coincides with any accessible tick price. An unprivileged attacker can monitor the fee, identify the matching tick, and post a buy offer there at negligible cost. Additionally, a governance-driven fee increase can retroactively turn existing offers into traps for the periphery. The attack is repeatable after each cancellation. No special privileges are required.

## Recommendation
Guard `sellerAssetsToUnits` against `sellerPrice == 0` explicitly, either by reverting early with a clear error (mirroring the `offerPrice < settlementFee` check), or by returning a sentinel value (e.g., `type(uint256).max`) that the caller can handle. The simplest fix is to add a `require(sellerPrice > 0)` check in `sellerAssetsToUnits` before calling `mulDivUp`, making the behavior consistent with the documented revert condition. Alternatively, move the `sellerAssetsToUnits` call inside the `try/catch` block so that a revert from the helper is caught and the offer is skipped rather than propagating to the caller.

## Proof of Concept
1. Deploy the protocol with a market where `settlementFee` for the relevant time bucket equals some value `F`.
2. Post a buy offer at tick `T` such that `tickToPrice(T) == F` (so `sellerPrice = 0`).
3. Call `supplyCollateralAndSellWithAssetsTarget` with `takes[0]` pointing to that offer and any nonzero `targetSellerAssets`.
4. Observe: `sellerAssetsToUnits` computes `sellerPrice = F - F = 0`, calls `mulDivUp(targetSellerAssets, WAD, 0)`, which evaluates `(0 - 1)` and reverts with an arithmetic underflow panic.
5. The entire transaction reverts despite `take()` itself being willing to accept the offer (returning `sellerAssets = 0`).

### Citations

**File:** src/libraries/UtilsLib.sol (L34-36)
```text
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
```

**File:** src/periphery/TakeAmountsLib.sol (L32-35)
```text
    /// @dev Forward: sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD).
    /// @dev Assumes that id and offer.market match.
    /// @dev Reverts if offerPrice < settlementFee in case of a buy offer (midnight reverts too).
    /// @dev Returns a number of units for which take yields exactly targetSellerAssets (not necessarily the smallest).
```

**File:** src/periphery/TakeAmountsLib.sol (L44-46)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        return
            offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

**File:** src/Midnight.sol (L361-364)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/periphery/MidnightBundles.sol (L175-175)
```text
    /// @dev Reverts if TakeAmountsLib or ConsumableUnitsLib reverts.
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
