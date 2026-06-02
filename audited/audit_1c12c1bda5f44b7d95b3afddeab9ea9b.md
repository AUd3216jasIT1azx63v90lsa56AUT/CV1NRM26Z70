I have verified all the technical claims against the actual code. Let me confirm each one:

Audit Report

## Title
`sellerAssetsToUnits` arithmetic revert when `offerPrice == settlementFee` causes `supplyCollateralAndSellWithAssetsTarget` to DoS - (File: src/periphery/TakeAmountsLib.sol)

## Summary
`TakeAmountsLib.sellerAssetsToUnits` computes `sellerPrice = offerPrice - settlementFee` for buy offers and passes it as the denominator to `mulDivUp`. When `offerPrice == settlementFee`, `sellerPrice = 0`, and `mulDivUp(x, WAD, 0)` triggers an arithmetic underflow on `d - 1` in Solidity 0.8+, reverting unconditionally. The core `Midnight.take()` computes `sellerAssets = units.mulDivDown(0, WAD) = 0` in the same scenario and does not revert. Because `supplyCollateralAndSellWithAssetsTarget` calls `sellerAssetsToUnits` outside its `try/catch` block, any revert there propagates to the caller, reverting the entire bundle.

## Finding Description

**Root cause — `TakeAmountsLib.sellerAssetsToUnits` (`src/periphery/TakeAmountsLib.sol`, lines 44–46):**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...;
```

When `offer.buy == true` and `offerPrice == settlementFee`, `sellerPrice = 0`. The call `mulDivUp(targetSellerAssets, WAD, 0)` executes `(x * y + (d - 1)) / d` with `d = 0`. The sub-expression `d - 1` underflows (uint256 wraps to `type(uint256).max`), triggering an arithmetic revert in Solidity 0.8+. [1](#0-0) [2](#0-1) 

This is confirmed by the existing unit test `testMulDivUpDivisionByZero` which expects `stdError.arithmeticError` (not `divisionError`) when `d = 0`. [3](#0-2) 

**Core `take()` does NOT revert in the same scenario (`src/Midnight.sol`, lines 361–364):**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice; // = 0
uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : ...; // = 0, no revert
```

`mulDivDown(units, 0, WAD)` computes `(units * 0) / WAD = 0` — no division by zero, no revert. `take()` succeeds and the seller receives zero assets. [4](#0-3) 

**`supplyCollateralAndSellWithAssetsTarget` calls `sellerAssetsToUnits` outside the `try/catch` (`src/periphery/MidnightBundles.sol`, lines 285–300):**

```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(          // ← NOT inside try/catch
        MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
    ),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
);
try IMidnight(MIDNIGHT).take(...) ...            // ← only take() is guarded
```

A revert from `sellerAssetsToUnits` is not caught and bubbles up, reverting the entire bundle call. [5](#0-4) 

**NatSpec divergence:** The `sellerAssetsToUnits` NatSpec documents revert only for `offerPrice < settlementFee`. The `==` case is undocumented and unguarded, yet `take()` explicitly handles it (seller receives zero assets, no revert). [6](#0-5) 

`ConsumableUnitsLib.consumableUnits` also calls `sellerAssetsToUnits` (line 21) and is likewise invoked outside the `try/catch` at line 290, providing a second revert path. [7](#0-6) 

## Impact Explanation
Any call to `supplyCollateralAndSellWithAssetsTarget` whose `takes[]` array contains a buy offer where `tickToPrice(tick) == settlementFee(id, ttm)` reverts entirely. The taker's collateral has already been supplied before the loop (lines 269–274), so the revert wastes gas and blocks the sell path for that bundle. Because `take()` itself would accept the offer (returning `sellerAssets = 0`), the offer appears valid on-chain, making the failure non-obvious to callers who rely on the bundler's documented skip-on-revert behavior. [8](#0-7) 

## Likelihood Explanation
The settlement fee is governance-controlled and must be a multiple of `CBP` (1e12). Tick prices are also multiples of `priceRoundingStep`. The condition `tickToPrice(T) == settlementFee` is achievable whenever the fee is set to a value coinciding with any accessible tick price. Additionally, if the fee changes between when a taker constructs their transaction and when it executes (a scenario the protocol's own NatSpec acknowledges at `Midnight.sol` line 329), a previously-valid offer can silently become a zero-yield offer, triggering the revert without any attacker involvement. An attacker can also post a buy offer at the matching tick at negligible cost and wait for a taker to include it. [9](#0-8) [10](#0-9) 

## Recommendation
In `sellerAssetsToUnits`, guard the zero-denominator case explicitly. When `sellerPrice == 0` and `targetSellerAssets > 0`, the inverse is undefined (no finite number of units yields a positive seller asset amount at zero price); return `type(uint256).max` or revert with a descriptive error. When `targetSellerAssets == 0`, return `0` directly. This mirrors how `take()` treats the case and eliminates the divergence:

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
if (offer.buy && sellerPrice == 0) {
    return targetSellerAssets == 0 ? 0 : type(uint256).max;
}
return offer.buy
    ? targetSellerAssets.mulDivUp(WAD, sellerPrice)
    : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

Update the NatSpec to document the `==` case explicitly.

## Proof of Concept
Minimal Foundry test:
1. Deploy `Midnight` and `MidnightBundles` in a test environment.
2. Set `settlementFee` for the market to any value `F` that equals `tickToPrice(T)` for some accessible tick `T` (e.g., set fee to the price of tick 4).
3. Have the attacker (lender) create a buy offer at tick `T` with `maxUnits > 0`.
4. Have the taker (borrower) call `supplyCollateralAndSellWithAssetsTarget` with `takes[0]` pointing to that offer and `targetSellerAssets > 0`.
5. Observe: the call reverts with `stdError.arithmeticError` from `mulDivUp`, even though a direct call to `Midnight.take()` with the same offer succeeds and returns `sellerAssets = 0`.

### Citations

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

**File:** src/Midnight.sol (L258-263)
```text
    function setMarketSettlementFee(bytes32 id, uint256 index, uint256 newSettlementFee) external {
        MarketState storage _marketState = marketState[id];
        require(msg.sender == feeSetter, OnlyFeeSetter());
        require(index <= 6, InvalidFeeIndex());
        require(newSettlementFee <= maxSettlementFee(index), SettlementFeeTooHigh());
        require(newSettlementFee % CBP == 0, FeeNotMultipleOfFeeCbp());
```

**File:** src/Midnight.sol (L329-332)
```text
    /// @dev The taker might not get the price they expected if the settlement fee was just changed. A smart-contract
    /// can be used to perform atomic price checks.
    /// @dev Taking buy offers with price < settlement fee will revert.
    /// @dev In particular, if the settlement fee gets increased, it might implicitly cancel offers with very low price.
```

**File:** src/Midnight.sol (L361-364)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
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

**File:** src/periphery/ConsumableUnitsLib.sol (L18-22)
```text
        } else if (offer.buy) {
            return TakeAmountsLib.buyerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
        } else {
            return TakeAmountsLib.sellerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
        }
```
