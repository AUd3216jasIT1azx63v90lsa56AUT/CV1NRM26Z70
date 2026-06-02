All code references check out. Here is the validation result:

---

Audit Report

## Title
`sellerAssetsToUnits` panics with arithmetic underflow when `offerPrice == settlementFee` for a buy offer, causing unconditional revert of `supplyCollateralAndSellWithAssetsTarget` - (File: src/periphery/TakeAmountsLib.sol)

## Summary

`TakeAmountsLib.sellerAssetsToUnits` computes `sellerPrice = offerPrice - settlementFee` for buy offers and passes it as the divisor to `mulDivUp`. When `offerPrice == settlementFee`, `sellerPrice = 0`, and `mulDivUp` panics via arithmetic underflow on `d - 1` before any division occurs. Because this call is placed outside the `try/catch` block in `supplyCollateralAndSellWithAssetsTarget`, the panic propagates and reverts the entire bundler transaction. The core `Midnight.take()` computes `sellerAssets = units.mulDivDown(0, WAD) = 0` in this case and succeeds, making this a concrete divergence between the periphery helper and the core protocol.

## Finding Description

**Root cause — `mulDivUp` with `d = 0`:**

`UtilsLib.mulDivUp` is:

```solidity
// src/libraries/UtilsLib.sol:34-36
function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y + (d - 1)) / d;
}
```

The `d - 1` term underflows before the division when `d = 0`, producing `Panic(0x11)`. This is confirmed by the existing test:

```solidity
// test/UtilsLibTest.sol:80-84
function testMulDivUpDivisionByZero(uint256 x, uint256 y) public {
    // because there is d-1.
    vm.expectRevert(stdError.arithmeticError);
    this.mulDivUp(x, y, 0);
}
```

**Trigger path in `sellerAssetsToUnits`:**

```solidity
// src/periphery/TakeAmountsLib.sol:44-46
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

When `offer.buy == true` and `offerPrice == settlementFee`, `sellerPrice = 0`, and `mulDivUp(targetSellerAssets, WAD, 0)` panics unconditionally regardless of `targetSellerAssets`.

The NatSpec at line 34 documents "Reverts if `offerPrice < settlementFee` in case of a buy offer (midnight reverts too)." The `offerPrice == settlementFee` case is undocumented and is a case where Midnight does **not** revert, but the helper does.

**Placement outside `try/catch` in the bundler:**

```solidity
// src/periphery/MidnightBundles.sol:285-300
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(          // ← outside try/catch
        MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
    ),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
);
try IMidnight(MIDNIGHT).take(...) returns (...) { ... } catch {}
```

The panic from `sellerAssetsToUnits` is not caught and reverts the entire `supplyCollateralAndSellWithAssetsTarget` call, including any collateral already supplied in the same transaction.

**Contrast with `Midnight.take()`:**

```solidity
// src/Midnight.sol:361-364
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
uint256 buyerPrice = sellerPrice + _settlementFee;
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

`mulDivDown(units, 0, WAD) = (units * 0) / WAD = 0`. No revert. The take succeeds with `sellerAssets = 0` and all buyer assets going to the settlement fee.

**Missing guard:** `buyerAssetsToUnits` uses `buyerPrice = sellerPrice + settlementFee` as its divisor, so when `sellerPrice = 0`, `buyerPrice = settlementFee > 0` and no panic occurs. `sellerAssetsToUnits` has no analogous protection.

## Impact Explanation

Any call to `supplyCollateralAndSellWithAssetsTarget` that includes a buy offer where `tickToPrice(offer.tick) == settlementFee(id, ttm)` reverts unconditionally with `Panic(0x11)`. The entire bundler transaction fails; collateral already supplied via `supplyCollateral` in the same call is not recoverable within that transaction. Direct calls to `Midnight.take()` are unaffected. The impact is a functional DoS of the bundler for a specific but reachable boundary condition, preventing users from using `supplyCollateralAndSellWithAssetsTarget` to fill such offers even though the core protocol would accept them.

## Likelihood Explanation

**Preconditions:**
1. A buy offer exists with `offer.tick` such that `tickToPrice(offer.tick)` equals the current `settlementFee(id, ttm)`.
2. Both tick prices and settlement fees are multiples of `CBP` (1e12), so exact equality is arithmetically achievable.
3. Settlement fees are tunable by governance via `setMarketSettlementFee` after offers are placed, meaning an existing offer can be moved to the boundary without the maker's knowledge.
4. The taker includes such an offer in the `takes[]` array passed to `supplyCollateralAndSellWithAssetsTarget`.

The condition is narrow but concrete: tick prices are discrete multiples of `1e12` and settlement fees are set in `1e12` increments, so the equality `tickToPrice(tick) == settlementFee` is achievable. A governance fee change can silently create this condition for existing offers.

## Recommendation

Add a guard in `sellerAssetsToUnits` for the `sellerPrice == 0` case. When `sellerPrice == 0`, any number of units yields `sellerAssets = 0`, so the function should return `type(uint256).max` (or a sentinel indicating "no finite unit count achieves a positive sellerAssets target") when `targetSellerAssets > 0`, or `0` when `targetSellerAssets == 0`. Alternatively, mirror the pattern from `buyerAssetsToUnits` and add an explicit `require(sellerPrice > 0)` guard with a descriptive error, consistent with the documented revert behavior. The NatSpec should also be updated to document the `offerPrice == settlementFee` case explicitly.

## Proof of Concept

Minimal Foundry test:

```solidity
// In a test extending BaseTest / TakeAmountsTest setup:
// 1. Create a market and touch it.
// 2. Set settlementFee such that settlementFee == tickToPrice(someTick).
// 3. Construct a buy offer at someTick.
// 4. Call TakeAmountsLib.sellerAssetsToUnits(midnight, id, buyOffer, 1e18).
// 5. Expect revert with stdError.arithmeticError (Panic 0x11).
// 6. Call Midnight.take(buyOffer, ..., 1e18, ...) directly.
// 7. Expect success with sellerAssets == 0.
```

The existing `testMulDivUpDivisionByZero` in `test/UtilsLibTest.sol` already confirms step 4–5 at the `mulDivUp` level. A targeted integration test combining `setMarketSettlementFee` to align the fee with a tick price, then calling `sellerAssetsToUnits` with a buy offer at that tick, would reproduce the panic end-to-end. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** src/libraries/UtilsLib.sol (L34-36)
```text
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
```

**File:** src/periphery/TakeAmountsLib.sol (L44-46)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        return
            offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
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

**File:** src/Midnight.sol (L258-275)
```text
    function setMarketSettlementFee(bytes32 id, uint256 index, uint256 newSettlementFee) external {
        MarketState storage _marketState = marketState[id];
        require(msg.sender == feeSetter, OnlyFeeSetter());
        require(index <= 6, InvalidFeeIndex());
        require(newSettlementFee <= maxSettlementFee(index), SettlementFeeTooHigh());
        require(newSettlementFee % CBP == 0, FeeNotMultipleOfFeeCbp());
        require(_marketState.tickSpacing > 0, MarketNotCreated());
        // forge-lint: disable-next-item(unsafe-typecast) as newSettlementFee <= maxSettlementFee <= uint16.max * CBP
        uint16 newSettlementFeeCbp = uint16(newSettlementFee / CBP);
        if (index == 0) _marketState.settlementFeeCbp0 = newSettlementFeeCbp;
        else if (index == 1) _marketState.settlementFeeCbp1 = newSettlementFeeCbp;
        else if (index == 2) _marketState.settlementFeeCbp2 = newSettlementFeeCbp;
        else if (index == 3) _marketState.settlementFeeCbp3 = newSettlementFeeCbp;
        else if (index == 4) _marketState.settlementFeeCbp4 = newSettlementFeeCbp;
        else if (index == 5) _marketState.settlementFeeCbp5 = newSettlementFeeCbp;
        else if (index == 6) _marketState.settlementFeeCbp6 = newSettlementFeeCbp;
        emit EventsLib.SetMarketSettlementFee(id, index, newSettlementFee);
    }
```

**File:** src/Midnight.sol (L361-364)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** test/UtilsLibTest.sol (L80-84)
```text
    function testMulDivUpDivisionByZero(uint256 x, uint256 y) public {
        // because there is d-1.
        vm.expectRevert(stdError.arithmeticError);
        this.mulDivUp(x, y, 0);
    }
```
