Audit Report

## Title
`sellerAssetsToUnits` reverts on division-by-zero when `offerPrice == settlementFee`, causing uncaught revert in `supplyCollateralAndSellWithAssetsTarget` - (File: src/periphery/TakeAmountsLib.sol)

## Summary
When `offer.buy = true` and `offerPrice == settlementFee`, `sellerAssetsToUnits` computes `sellerPrice = 0` and calls `mulDivUp(targetSellerAssets, WAD, 0)`. The `mulDivUp` implementation evaluates `d - 1` before dividing, causing an arithmetic underflow revert under Solidity 0.8 checked arithmetic. Because `sellerAssetsToUnits` is invoked outside the `try/catch` block in `supplyCollateralAndSellWithAssetsTarget`, the revert propagates uncaught and the entire bundler call fails rather than skipping the problematic offer.

## Finding Description

**Root cause — `sellerAssetsToUnits`:**

In `src/periphery/TakeAmountsLib.sol` lines 44–46:
```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...
```
When `offer.buy = true` and `offerPrice == settlementFee`, `sellerPrice = 0`, and the call becomes `mulDivUp(targetSellerAssets, WAD, 0)`. [1](#0-0) 

**`mulDivUp` implementation:**

In `src/libraries/UtilsLib.sol` line 35:
```solidity
return (x * y + (d - 1)) / d;
```
With `d = 0`, the sub-expression `d - 1` = `0 - 1` underflows under Solidity 0.8 checked arithmetic, reverting with `stdError.arithmeticError` before any division occurs. [2](#0-1) 

**Test suite confirms this exact behavior:**

`test/UtilsLibTest.sol` lines 80–84 explicitly documents and tests this revert path:
```solidity
function testMulDivUpDivisionByZero(uint256 x, uint256 y) public {
    // because there is d-1.
    vm.expectRevert(stdError.arithmeticError);
    this.mulDivUp(x, y, 0);
}
``` [3](#0-2) 

**Uncaught revert path in `supplyCollateralAndSellWithAssetsTarget`:**

In `src/periphery/MidnightBundles.sol` lines 285–291, `TakeAmountsLib.sellerAssetsToUnits(...)` is evaluated as an argument to `min(...)` at line 286, **before** the `try` block at line 292. The `try/catch` only wraps `IMidnight(MIDNIGHT).take(...)`. The revert from `sellerAssetsToUnits` propagates uncaught, reverting the entire function. [4](#0-3) 

**NatDoc gap in `sellerAssetsToUnits`:**

The function's NatDoc at line 34 states: *"Reverts if offerPrice < settlementFee in case of a buy offer"* — the equality case (`offerPrice == settlementFee`) is not documented as a revert condition, yet it triggers one. [5](#0-4) 

**Contrast with `buyerAssetsToUnits`:**

`buyerAssetsToUnits` is not affected: when `sellerPrice = 0`, it computes `buyerPrice = 0 + settlementFee = settlementFee > 0` and calls `mulDivUp(targetBuyerAssets, WAD, settlementFee)`, which is safe. [6](#0-5) 

**Correction to submitted impact claim:** The claim that "collateral already supplied is locked" is incorrect. Because the revert propagates through the entire transaction, all state changes — including the collateral supply at lines 269–275 — are atomically reverted. No funds are locked; the DoS impact is that the function is entirely non-functional for any call that includes such an offer. [7](#0-6) 

## Impact Explanation

Any call to `supplyCollateralAndSellWithAssetsTarget` that includes a buy offer where `offerPrice == settlementFee` reverts entirely. Borrowers relying on this bundler function to sell units and service their debt are blocked for the duration the condition holds. If the settlement fee is raised to match multiple outstanding offers simultaneously, the bundler is non-functional for all of them at once. The impact is a targeted, repeatable DoS on a core borrower-facing bundler function. The contract's own NatDoc (line 246) acknowledges "Reverts if TakeAmountsLib or ConsumableUnitsLib reverts," but the `sellerAssetsToUnits` NatDoc does not document the equality case as a revert condition, creating an undocumented and unexpected failure mode. [8](#0-7) 

## Likelihood Explanation

The condition `offerPrice == settlementFee` is reachable in two ways: (1) a maker deliberately places a buy offer at the tick whose price equals the current settlement fee — tick prices and settlement fee values are both discrete, making exact equality achievable; (2) the `feeSetter` admin raises the settlement fee post-offer-creation to a value that coincidentally matches an existing offer's price, a normal operational action. The condition is transient (it resolves as time-to-maturity shifts the interpolated fee), but it can be re-triggered repeatedly. No special privilege is required for path (1); any unprivileged maker can create such an offer.

## Recommendation

Add a guard in `sellerAssetsToUnits` for the `sellerPrice == 0` case. Since a `sellerPrice` of zero means the offer yields nothing to the seller, the appropriate return value is `type(uint256).max` (indicating no finite number of units can satisfy the target), which will cause `min(...)` in the bundler to select zero units and the offer will be effectively skipped. Alternatively, add an explicit `require(sellerPrice > 0)` with a descriptive error, and update the NatDoc to document the equality case as a revert condition. The `sellerAssetsToUnits` NatDoc should be updated to read: *"Reverts if offerPrice ≤ settlementFee in case of a buy offer."*

## Proof of Concept

1. Deploy Midnight and MidnightBundles on a fork.
2. Create a buy offer at a tick `t` such that `TickLib.tickToPrice(t) == IMidnight.settlementFee(id, timeToMaturity)`.
3. Call `supplyCollateralAndSellWithAssetsTarget` with `takes` containing only this offer.
4. Observe the transaction reverts with `stdError.arithmeticError` rather than skipping the offer and reverting with `OutOfOffers`.
5. Confirm by unit test: mock `settlementFee` to return `tickToPrice(offer.tick)`, call `sellerAssetsToUnits` directly, and assert it reverts with `arithmeticError`.

### Citations

**File:** src/periphery/TakeAmountsLib.sol (L26-29)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + settlementFee;
        require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne());
        return offer.buy ? targetBuyerAssets.mulDivUp(WAD, buyerPrice) : targetBuyerAssets.mulDivDown(WAD, buyerPrice);
```

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

**File:** test/UtilsLibTest.sol (L80-84)
```text
    function testMulDivUpDivisionByZero(uint256 x, uint256 y) public {
        // because there is d-1.
        vm.expectRevert(stdError.arithmeticError);
        this.mulDivUp(x, y, 0);
    }
```

**File:** src/periphery/MidnightBundles.sol (L246-246)
```text
    /// @dev Reverts if TakeAmountsLib or ConsumableUnitsLib reverts.
```

**File:** src/periphery/MidnightBundles.sol (L269-275)
```text
        for (uint256 i; i < collateralSupplies.length; i++) {
            address token = market.collateralParams[collateralSupplies[i].collateralIndex].token;
            pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
            forceApproveMax(token, MIDNIGHT);
            IMidnight(MIDNIGHT)
                .supplyCollateral(market, collateralSupplies[i].collateralIndex, collateralSupplies[i].assets, taker);
        }
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
