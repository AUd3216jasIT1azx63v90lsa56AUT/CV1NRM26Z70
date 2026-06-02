Audit Report

## Title
`sellerAssetsToUnits` reverts on division-by-zero when `offerPrice == settlementFee`, DoS-ing `supplyCollateralAndSellWithAssetsTarget` - (File: src/periphery/TakeAmountsLib.sol)

## Summary
`TakeAmountsLib.sellerAssetsToUnits` computes `sellerPrice = offerPrice - settlementFee` for buy offers and passes it as the divisor `d` to `mulDivUp`. When `offerPrice == settlementFee`, `sellerPrice = 0`, and `mulDivUp`'s expression `(d - 1)` underflows in Solidity 0.8+, causing an unconditional revert. Because `supplyCollateralAndSellWithAssetsTarget` calls `sellerAssetsToUnits` outside its `try/catch` block, the revert propagates to the caller, DoS-ing that entry-point for any bundle whose `takes[]` array includes such an offer. The core `Midnight.take()` handles this case without reverting.

## Finding Description

**Root cause — `TakeAmountsLib.sellerAssetsToUnits` (`src/periphery/TakeAmountsLib.sol` lines 44–46):** [1](#0-0) 

When `offer.buy == true` and `offerPrice == settlementFee`, `sellerPrice = 0`. The call becomes `targetSellerAssets.mulDivUp(WAD, 0)`.

**`mulDivUp` underflows with `d = 0` (`src/libraries/UtilsLib.sol` lines 34–36):** [2](#0-1) 

With `d = 0`, the sub-expression `(d - 1)` = `(0 - 1)` underflows under Solidity 0.8+ checked arithmetic, reverting unconditionally before the division executes.

**Core `Midnight.take()` does NOT revert in the same scenario (`src/Midnight.sol` lines 361–364):** [3](#0-2) 

`mulDivDown(units, 0, WAD)` = `(units * 0) / WAD = 0`. No underflow, no revert. The seller simply receives zero assets.

**`supplyCollateralAndSellWithAssetsTarget` calls `sellerAssetsToUnits` outside the `try/catch` (`src/periphery/MidnightBundles.sol` lines 285–300):** [4](#0-3) 

The NatSpec at line 246 explicitly acknowledges: *"Reverts if TakeAmountsLib or ConsumableUnitsLib reverts."* The `==` case is undocumented in `sellerAssetsToUnits`'s NatSpec (line 34 only documents `offerPrice < settlementFee`), making it an unguarded revert path. [5](#0-4) 

**Existing protections are insufficient:** The `try/catch` only wraps `IMidnight.take()`. The `sellerAssetsToUnits` call preceding it is unguarded. The `offerPrice < settlementFee` case reverts via underflow at line 44 (`offerPrice - settlementFee`), which is documented. The `offerPrice == settlementFee` case passes line 44 silently (result is 0) but reverts inside `mulDivUp`, which is undocumented and unguarded.

## Impact Explanation
Any call to `supplyCollateralAndSellWithAssetsTarget` whose `takes[]` array contains a buy offer where `tickToPrice(offer.tick) == settlementFee` reverts entirely. An attacker can grief any taker who includes that offer, permanently blocking the periphery sell path for that bundle. Because the offer itself is valid and `Midnight.take()` would accept it (seller receives zero assets), the DoS is invisible to the taker until the bundle reverts. This constitutes a griefing/DoS of a core periphery entry-point with no capital cost to the attacker.

## Likelihood Explanation
Settlement fees are discrete governance-set values; tick prices are also discrete. The condition `tickToPrice(T) == settlementFee` is achievable whenever the fee coincides with any accessible tick price. An unprivileged attacker can monitor the fee, identify the matching tick, and post a buy offer there at negligible cost (a buy offer with `maxUnits = 1` requires minimal capital). The attack is repeatable after each cancellation. The victim's off-chain routing may include this offer if it appears as the best or only available offer at that price level.

## Recommendation
Handle the `sellerPrice == 0` case explicitly in `sellerAssetsToUnits`. When `sellerPrice == 0`, the seller receives zero assets for any number of units, so the function should return `type(uint256).max` (no finite unit count yields a positive seller asset target) or `0` if `targetSellerAssets == 0`. A minimal fix:

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
if (sellerPrice == 0) return type(uint256).max; // no units yield positive seller assets
return offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...;
```

Alternatively, wrap the `sellerAssetsToUnits` call in `supplyCollateralAndSellWithAssetsTarget` in a `try/catch` consistent with how `take()` is handled, skipping offers that cause a revert.

## Proof of Concept
1. Deploy on a fork with a market where `settlementFee(id, timeToMaturity) = F`.
2. Identify tick `T` such that `tickToPrice(T) == F`.
3. Attacker posts a buy offer at tick `T` with `maxUnits > 0`.
4. Victim calls `supplyCollateralAndSellWithAssetsTarget` with `takes[0]` pointing to the attacker's offer.
5. Execution reaches line 286: `TakeAmountsLib.sellerAssetsToUnits(...)` computes `sellerPrice = F - F = 0`, then calls `mulDivUp(WAD, 0)`, hitting `(0 - 1)` underflow → revert.
6. The entire bundle reverts. Calling `Midnight.take()` directly on the same offer succeeds (seller receives 0 assets).

### Citations

**File:** src/periphery/TakeAmountsLib.sol (L34-35)
```text
    /// @dev Reverts if offerPrice < settlementFee in case of a buy offer (midnight reverts too).
    /// @dev Returns a number of units for which take yields exactly targetSellerAssets (not necessarily the smallest).
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
