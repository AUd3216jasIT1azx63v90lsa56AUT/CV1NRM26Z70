Audit Report

## Title
`sellerAssetsToUnits` panics with arithmetic underflow when `offerPrice == settlementFee` for a buy offer, causing unconditional revert of `supplyCollateralAndSellWithAssetsTarget` - (File: src/periphery/TakeAmountsLib.sol)

## Summary

`TakeAmountsLib.sellerAssetsToUnits` computes `sellerPrice = offerPrice - settlementFee` for buy offers and then calls `mulDivUp(targetSellerAssets, WAD, sellerPrice)`. When `offerPrice == settlementFee`, `sellerPrice` is exactly `0`, and `mulDivUp` panics via arithmetic underflow on `d - 1` before any division occurs. This call is placed outside the `try/catch` block in `supplyCollateralAndSellWithAssetsTarget`, so the panic propagates and reverts the entire bundler call. The core `Midnight.take()` handles `sellerPrice == 0` gracefully (yielding `sellerAssets = 0`), making this a divergence between the periphery helper and the core protocol.

## Finding Description

**Root cause — `mulDivUp` with `d = 0`:**

`UtilsLib.mulDivUp` is implemented as:

```solidity
function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y + (d - 1)) / d;   // d=0: (d-1) underflows → Panic(0x11)
}
```

The `d - 1` term underflows before the division when `d = 0`. This is confirmed by the existing test:

```solidity
function testMulDivUpDivisionByZero(uint256 x, uint256 y) public {
    // because there is d-1.
    vm.expectRevert(stdError.arithmeticError);
    this.mulDivUp(x, y, 0);
}
```

**Trigger path in `sellerAssetsToUnits`:**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...;
```

When `offerPrice == settlementFee`, `sellerPrice = 0`, and `mulDivUp(targetSellerAssets, WAD, 0)` panics unconditionally regardless of `targetSellerAssets`.

**Placement outside `try/catch` in the bundler:**

```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(          // ← outside try/catch
        MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
    ),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
);
try IMidnight(MIDNIGHT).take(...) returns (...) { ... } catch {}
```

The panic from `sellerAssetsToUnits` is not caught and reverts the entire `supplyCollateralAndSellWithAssetsTarget` call.

**Contrast with `Midnight.take()`:**

In the core protocol, when `sellerPrice = 0`:

```solidity
uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : ...;
// = units.mulDivDown(0, WAD) = 0  → no revert
```

`mulDivDown(x, 0, WAD) = (x * 0) / WAD = 0`. The take succeeds with `sellerAssets = 0`. This is explicitly tested and documented in `testPriceZeroWithSettlementFeeSell` (for sell offers with fee > 0, `sellerPrice = 0` is valid).

**NatSpec discrepancy:** `sellerAssetsToUnits` documents "Reverts if `offerPrice < settlementFee` in case of a buy offer (midnight reverts too)." The `offerPrice == settlementFee` case is undocumented and is a case where Midnight does **not** revert, but the helper does.

**Missing guard:** `buyerAssetsToUnits` has `require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne())` as an explicit guard. `sellerAssetsToUnits` has no analogous `require(sellerPrice > 0)` guard.

## Impact Explanation

Any call to `supplyCollateralAndSellWithAssetsTarget` that includes a buy offer where `tickToPrice(offer.tick) == settlementFee(id, ttm)` will revert unconditionally with `Panic(0x11)`. The entire bundler transaction fails; collateral already supplied in the same call is not recoverable within that transaction. Direct calls to `Midnight.take()` are unaffected. The impact is a functional DoS of the bundler for a specific but reachable boundary condition, preventing users from using `supplyCollateralAndSellWithAssetsTarget` to fill such offers even though the core protocol would accept them.

## Likelihood Explanation

**Preconditions:**
1. A buy offer exists with `offer.tick` such that `tickToPrice(offer.tick)` equals the current `settlementFee(id, ttm)`.
2. Both tick prices and settlement fees are multiples of `1e12` (CBP), so exact equality is arithmetically achievable.
3. Settlement fees are tunable by governance via `setMarketSettlementFee` after offers are placed, meaning an existing offer can be moved to the boundary without the maker's knowledge.
4. The taker includes such an offer in the `takes[]` array passed to `supplyCollateralAndSellWithAssetsTarget`.

The condition is narrow but reachable: tick prices are discrete multiples of `1e12` and settlement fees are set in `1e12` increments, so the equality `tickToPrice(tick) == settlementFee` is a concrete, achievable state. A governance fee change can silently create this condition for existing offers.

## Recommendation

Add an explicit guard in `sellerAssetsToUnits` for the `sellerPrice == 0` case. Since no finite number of units can yield a positive `targetSellerAssets` when `sellerPrice == 0`, the function should return `type(uint256).max` (signaling "impossible to reach target"), which causes the `min(...)` in the bundler to fall back to `takes[i].units` or `consumableUnits`, and the subsequent `take` call (which would yield `sellerAssets = 0`) would be skipped by the loop's progress check:

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
if (offer.buy && sellerPrice == 0) return type(uint256).max;
return offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...;
```

Alternatively, align the NatSpec to document that the function also reverts when `offerPrice == settlementFee`, and add a pre-check in the bundler loop to skip such offers rather than panicking.

## Proof of Concept

```solidity
// Minimal Foundry test
function testSellerAssetsToUnitsPanicsAtBoundary() public {
    // Set settlement fee to exactly tickToPrice(tick) for some tick
    uint256 tick = DEFAULT_TICK_SPACING; // pick any tick with price > 0
    uint256 price = TickLib.tickToPrice(tick);
    // price is a multiple of 1e12; set settlement fee to match
    midnight.touchMarket(market);
    midnight.setMarketSettlementFee(id, 1, price); // offerPrice == settlementFee

    Offer memory buyOffer = ...; // buy=true, tick=tick
    // This panics with stdError.arithmeticError:
    vm.expectRevert(stdError.arithmeticError);
    TakeAmountsLib.sellerAssetsToUnits(address(midnight), id, buyOffer, 1e18);
}
```

To confirm the bundler-level DoS, wrap the above in a call to `supplyCollateralAndSellWithAssetsTarget` with the boundary offer in `takes[]` and observe the entire call reverts rather than skipping the offer. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** src/periphery/TakeAmountsLib.sol (L32-34)
```text
    /// @dev Forward: sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD).
    /// @dev Assumes that id and offer.market match.
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

**File:** src/Midnight.sol (L361-364)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** test/TakeTest.sol (L1201-1221)
```text
    // Summary of zero price tests:
    //
    // Settlement at 0 succeeds in those cases:
    // - any offer / unit take input / 0 settlement fee.
    // - sell offer / unit take input / > 0 settlement fee.
    //
    // Otherwise it fails:
    // - by underflow when the settlement fee is > 0, and the offer is a buy offer.

    // fee=0, sell, units
    function testPriceZeroNoSettlementFeeSell() public {
        uint256 units = 1e18;
        borrowerOffer.tick = 0;
        borrowerOffer.maxUnits = units;
        collateralize(market, borrower, units);
        (uint256 buyerAssets, uint256 sellerAssets) = take(units, lender, borrowerOffer);
        assertEq(buyerAssets, 0, "buyerAssets");
        assertEq(sellerAssets, 0, "sellerAssets");
        assertEq(midnight.creditOf(id, lender), units, "creditOf");
        assertEq(midnight.debtOf(id, borrower), units, "debtOf");
    }
```
