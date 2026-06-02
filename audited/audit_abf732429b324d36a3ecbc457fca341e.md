All key claims are confirmed by the code. Let me summarize the validation:

**Root cause confirmed:** `UtilsLib.mulDivUp` at `src/libraries/UtilsLib.sol:35` computes `(x * y + (d - 1)) / d`; when `d = 0`, `d - 1` underflows → `Panic(0x11)`. This is explicitly tested in `testMulDivUpDivisionByZero`.

**Trigger path confirmed:** `sellerAssetsToUnits` at `src/periphery/TakeAmountsLib.sol:44-46` computes `sellerPrice = offerPrice - settlementFee` for buy offers, then calls `mulDivUp(WAD, sellerPrice)`. When `offerPrice == settlementFee`, `sellerPrice = 0` → panic.

**Placement outside try/catch confirmed:** `src/periphery/MidnightBundles.sol:285-291` calls `TakeAmountsLib.sellerAssetsToUnits(...)` inside the `min(...)` expression that feeds `unitsToTake`, which is computed before the `try IMidnight(MIDNIGHT).take(...)` block at line 292.

**Core divergence confirmed:** `src/Midnight.sol:361,364` — when `sellerPrice = 0` for a buy offer, `sellerAssets = units.mulDivDown(0, WAD) = 0`, no revert. `testPriceZeroWithSettlementFeeSell` (line 1236) confirms `sellerPrice = 0` is valid in the core for sell offers; `testPriceZeroWithSettlementFeeBuy` (line 1224) only tests `offerPrice < settlementFee` (underflow), not `offerPrice == settlementFee`.

**NatSpec discrepancy confirmed:** `sellerAssetsToUnits` documents "Reverts if `offerPrice < settlementFee`" but the `==` case is undocumented and diverges from core behavior.

---

Audit Report

## Title
`sellerAssetsToUnits` panics with arithmetic underflow when `offerPrice == settlementFee` for a buy offer, causing unconditional revert of `supplyCollateralAndSellWithAssetsTarget` - (File: src/periphery/TakeAmountsLib.sol)

## Summary
`TakeAmountsLib.sellerAssetsToUnits` computes `sellerPrice = offerPrice - settlementFee` for buy offers and passes it as the denominator to `mulDivUp(targetSellerAssets, WAD, sellerPrice)`. When `offerPrice == settlementFee`, `sellerPrice = 0` and `mulDivUp` panics via arithmetic underflow on `d - 1` before any division. This call is placed outside the `try/catch` block in `supplyCollateralAndSellWithAssetsTarget`, so the panic propagates and reverts the entire bundler call. The core `Midnight.take()` handles `sellerPrice == 0` gracefully with `sellerAssets = 0`, making this a confirmed divergence between the periphery helper and the core protocol.

## Finding Description

**Root cause — `mulDivUp` with `d = 0`:**

`UtilsLib.mulDivUp` is implemented as:

```solidity
// src/libraries/UtilsLib.sol:34-36
function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y + (d - 1)) / d;   // d=0: (d-1) underflows → Panic(0x11)
}
``` [1](#0-0) 

The `d - 1` term underflows before the division when `d = 0`. This is confirmed by the existing test:

```solidity
// test/UtilsLibTest.sol:80-84
function testMulDivUpDivisionByZero(uint256 x, uint256 y) public {
    // because there is d-1.
    vm.expectRevert(stdError.arithmeticError);
    this.mulDivUp(x, y, 0);
}
``` [2](#0-1) 

**Trigger path in `sellerAssetsToUnits`:**

```solidity
// src/periphery/TakeAmountsLib.sol:44-46
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...;
``` [3](#0-2) 

When `offerPrice == settlementFee`, `sellerPrice = 0`, and `mulDivUp(targetSellerAssets, WAD, 0)` panics unconditionally regardless of `targetSellerAssets`.

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
``` [4](#0-3) 

The panic from `sellerAssetsToUnits` is not caught and reverts the entire `supplyCollateralAndSellWithAssetsTarget` call.

**Contrast with `Midnight.take()`:**

In the core protocol, when `sellerPrice = 0` for a buy offer:

```solidity
// src/Midnight.sol:361,364
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : ...;
// = units.mulDivDown(0, WAD) = (units * 0) / WAD = 0  → no revert
``` [5](#0-4) 

`mulDivDown(x, 0, WAD) = 0`. The take succeeds with `sellerAssets = 0`. The test `testPriceZeroWithSettlementFeeSell` (line 1236) confirms `sellerPrice = 0` is valid in the core for sell offers; the `offerPrice == settlementFee` boundary for buy offers is untested and unguarded in the core. [6](#0-5) 

**NatSpec discrepancy:** `sellerAssetsToUnits` documents "Reverts if `offerPrice < settlementFee` in case of a buy offer (midnight reverts too)." The `offerPrice == settlementFee` case is undocumented and is a case where Midnight does **not** revert, but the helper does. [7](#0-6) 

**Missing guard:** `buyerAssetsToUnits` has `require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne())` as an explicit guard. `sellerAssetsToUnits` has no analogous `require(sellerPrice > 0)` guard. [8](#0-7) 

## Impact Explanation

Any call to `supplyCollateralAndSellWithAssetsTarget` that includes a buy offer where `tickToPrice(offer.tick) == settlementFee(id, ttm)` will revert unconditionally with `Panic(0x11)`. Since the entire transaction reverts atomically, no collateral is permanently lost — but the bundler function is completely unusable for this boundary condition. Direct calls to `Midnight.take()` are unaffected. The impact is a functional DoS of the bundler for a specific but reachable boundary condition, preventing users from using `supplyCollateralAndSellWithAssetsTarget` to fill such offers even though the core protocol would accept them.

## Likelihood Explanation

**Preconditions:**
1. A buy offer exists with `offer.tick` such that `tickToPrice(offer.tick)` equals the current `settlementFee(id, ttm)`.
2. Both tick prices and settlement fees are multiples of `1e12` (CBP), so exact equality is arithmetically achievable.
3. Settlement fees are tunable by governance via `setMarketSettlementFee` after offers are placed, meaning an existing offer can be moved to the boundary without the maker's knowledge.
4. The taker includes such an offer in the `takes[]` array passed to `supplyCollateralAndSellWithAssetsTarget`.

The condition is narrow but reachable: tick prices are discrete multiples of `1e12` and settlement fees are set in `1e12` increments, so the equality `tickToPrice(tick) == settlementFee` is a concrete, achievable state. A governance fee change can silently create this condition for existing offers, and any unprivileged user who then calls the bundler with such an offer triggers the DoS.

## Recommendation

Add an explicit guard in `sellerAssetsToUnits` to handle `sellerPrice == 0` for buy offers, mirroring the core's behavior of returning 0 seller assets (and thus 0 units needed):

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
if (offer.buy && sellerPrice == 0) return 0;
return offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

Alternatively, update the NatSpec to explicitly document that `offerPrice == settlementFee` also causes a revert, and ensure callers handle this case. The former approach is preferred as it aligns the periphery helper with core protocol behavior.

## Proof of Concept

**Minimal Foundry test:**

```solidity
function testSellerAssetsToUnitsPanicsWhenPriceEqualsSettlementFee() public {
    // Set settlement fee equal to tickToPrice(MAX_TICK)
    uint256 price = TickLib.tickToPrice(MAX_TICK);
    midnight.touchMarket(market);
    // Set fee to exactly match the offer price (requires finding the right breakpoint)
    midnight.setMarketSettlementFee(id, /* breakpoint */, price);
    
    // Confirm sellerPrice == 0
    uint256 fee = midnight.settlementFee(id, market.maturity - block.timestamp);
    assertEq(fee, price);
    
    // sellerAssetsToUnits panics
    vm.expectRevert(stdError.arithmeticError);
    TakeAmountsLib.sellerAssetsToUnits(address(midnight), id, lenderOffer, 1e18);
}
```

**Manual steps:**
1. Deploy Midnight and create a market.
2. Set `settlementFee` for the relevant TTM breakpoint to equal `tickToPrice(offer.tick)` for a buy offer.
3. Call `supplyCollateralAndSellWithAssetsTarget` with that buy offer in the `takes[]` array.
4. Observe `Panic(0x11)` (arithmetic underflow) revert — the bundler call fails entirely.

### Citations

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

**File:** src/periphery/TakeAmountsLib.sol (L26-29)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + settlementFee;
        require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne());
        return offer.buy ? targetBuyerAssets.mulDivUp(WAD, buyerPrice) : targetBuyerAssets.mulDivDown(WAD, buyerPrice);
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

**File:** test/TakeTest.sol (L1236-1251)
```text
    function testPriceZeroWithSettlementFeeSell() public {
        midnight.touchMarket(market);
        midnight.setMarketSettlementFee(id, 1, 1e12);
        uint256 fee = midnight.settlementFee(id, market.maturity - vm.getBlockTimestamp());
        uint256 units = 1e18;
        borrowerOffer.tick = 0;
        borrowerOffer.maxUnits = units;
        uint256 expectedBuyerAssets = units.mulDivUp(fee, WAD);
        deal(address(loanToken), lender, expectedBuyerAssets);
        collateralize(market, borrower, units);
        (uint256 buyerAssets, uint256 sellerAssets) = take(units, lender, borrowerOffer);
        assertEq(buyerAssets, expectedBuyerAssets, "buyerAssets");
        assertEq(sellerAssets, 0, "sellerAssets");
        assertEq(midnight.creditOf(id, lender), units, "creditOf");
        assertEq(midnight.debtOf(id, borrower), units, "debtOf");
    }
```
