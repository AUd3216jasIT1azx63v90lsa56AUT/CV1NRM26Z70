All key claims check out against the code. Here is the full validation:

**`tickToPrice(0) == 0`**: Confirmed by `test/TickLibTest.sol:16` and the Certora spec rule `tickToPriceIsZeroAtZero`.

**`mulDivDown(x, WAD, 0)` reverts**: `mulDivDown` is `(x * y) / d` with no `unchecked` block; Solidity 0.8 panics on division by zero. Confirmed by `test/UtilsLibTest.sol:58-63`.

**`buyerAssetsToUnits` trace with `tick=0`, `settlementFee=0`, `offer.buy=false`**: `offerPrice=0` → `sellerPrice=0` → `buyerPrice=0` → `require(0 <= WAD)` passes → `targetBuyerAssets.mulDivDown(WAD, 0)` → panic.

**`Midnight.take` does not revert**: `units.mulDivUp(0, WAD)` = `(0 + WAD-1)/WAD = 0`. No division by zero.

**Call site is outside try/catch**: Lines 209-213 call `buyerAssetsToUnits` before the `try` at line 215; a revert there propagates to the caller.

**`NoDivisionByZero` Certora spec**: Only covers `Midnight.sol`, not `TakeAmountsLib`.

---

Audit Report

## Title
Division-by-Zero in `buyerAssetsToUnits` on Zero-Price Sell Offer Causes Bundler DoS - (File: src/periphery/TakeAmountsLib.sol)

## Summary
When a sell offer has `tick=0` and the market's `settlementFee=0`, `TickLib.tickToPrice(0)` returns `0`, making `buyerPrice=0`. `buyerAssetsToUnits` then executes `targetBuyerAssets.mulDivDown(WAD, 0)`, which is `(targetBuyerAssets * WAD) / 0` and panics. The core `Midnight.take` path handles the same inputs without reverting. Because the `buyerAssetsToUnits` call in `buyWithAssetsTargetAndWithdrawCollateral` sits outside the `try/catch` block, the entire bundler transaction reverts.

## Finding Description
`TickLib.tickToPrice(0)` is confirmed to return `0` by both the existing unit test and the Certora spec rule `tickToPriceIsZeroAtZero`. [1](#0-0) [2](#0-1) 

In `buyerAssetsToUnits`, with `offer.buy=false`, `tick=0`, and `settlementFee=0`:
- `offerPrice = 0`
- `sellerPrice = offerPrice = 0` (sell-offer branch)
- `buyerPrice = 0 + 0 = 0`
- `require(0 <= WAD)` passes
- `return targetBuyerAssets.mulDivDown(WAD, 0)` → `(targetBuyerAssets * WAD) / 0` → Solidity 0.8 division-by-zero panic [3](#0-2) 

`mulDivDown` is a plain `(x * y) / d` with no `unchecked` wrapper, so `d=0` panics unconditionally. [4](#0-3) 

In `Midnight.take` the same inputs produce `units.mulDivUp(0, WAD) = (0 + WAD-1)/WAD = 0`, which succeeds. [5](#0-4) 

In `buyWithAssetsTargetAndWithdrawCollateral`, the `buyerAssetsToUnits` call at lines 209-211 is evaluated before the `try` at line 215. A revert inside `buyerAssetsToUnits` is not caught and propagates to the caller, unwinding the `pullToken` at line 197 (no fund loss, but the call fails entirely). [6](#0-5) 

The `NoDivisionByZero` Certora spec only verifies `Midnight.sol` and explicitly excludes `TakeAmountsLib` from its scope. [7](#0-6) 

## Impact Explanation
Any call to `buyWithAssetsTargetAndWithdrawCollateral` that includes a sell offer with `tick=0` and a market where `settlementFee=0` will revert unconditionally when `targetBuyerAssets > 0`. The bundler's asset-denominated targeting functionality is completely unavailable for zero-price offers. Callers must fall back to `Midnight.take` directly with a `units` input, losing the convenience and composability of the bundler path. Funds are not permanently frozen (the `pullToken` is unwound on revert), so the impact is a targeted DoS of the periphery contract.

## Likelihood Explanation
The preconditions are minimal and fully attacker-controllable. Any unprivileged borrower can post a sell offer with `tick=0`. Settlement fee is a market-level parameter; the existing bundler tests explicitly set it to `0` for multiple breakpoints, confirming this is a common and expected market configuration. [8](#0-7) 
The condition is stable and repeatable: every call to `buyWithAssetsTargetAndWithdrawCollateral` targeting such an offer will revert.

## Recommendation
Add a zero-price guard in `buyerAssetsToUnits` before the `mulDivDown` call. If `buyerPrice == 0`, the function should either revert with a descriptive error (e.g., `ZeroPrice()`) or return `type(uint256).max` to signal that no finite unit count can achieve a positive `targetBuyerAssets`. The same guard should be applied to `sellerAssetsToUnits` for the analogous `sellerPrice == 0` case. [9](#0-8) 

## Proof of Concept
```solidity
// Minimal Foundry test
function testBuyerAssetsToUnitsZeroPriceDivByZero() public {
    // Set settlement fee to 0 for all breakpoints
    for (uint256 i = 0; i <= 6; i++) {
        midnight.setMarketSettlementFee(id, i, 0);
    }
    // Confirm tickToPrice(0) == 0
    assertEq(TickLib.tickToPrice(0), 0);

    // Construct a sell offer at tick 0
    Offer memory sellOffer = /* ... standard sell offer ... */;
    sellOffer.tick = 0;
    sellOffer.buy = false;

    // buyerAssetsToUnits should revert with division-by-zero
    vm.expectRevert(stdError.divisionError);
    TakeAmountsLib.buyerAssetsToUnits(address(midnight), id, sellOffer, 1e18);
}
```
The same scenario can be triggered end-to-end by calling `buyWithAssetsTargetAndWithdrawCollateral` with a single sell offer at `tick=0` in a zero-settlement-fee market with `targetBuyerAssets > 0`.

### Citations

**File:** test/TickLibTest.sol (L15-16)
```text
    function testTickToPriceMinMax() public pure {
        assertEq(TickLib.tickToPrice(0), 0, "tick 0");
```

**File:** certora/specs/TickToPrice.spec (L34-36)
```text
rule tickToPriceIsZeroAtZero() {
    assert tickToPrice(0) == 0;
}
```

**File:** src/periphery/TakeAmountsLib.sol (L17-30)
```text
    function buyerAssetsToUnits(address midnight, bytes32 id, Offer memory offer, uint256 targetBuyerAssets)
        internal
        view
        returns (uint256)
    {
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
        uint256 settlementFee =
            IMidnight(midnight).settlementFee(id, UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp));
        // Mirrors Midnight's computation to revert if offerPrice < settlementFee in case of a buy offer.
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + settlementFee;
        require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne());
        return offer.buy ? targetBuyerAssets.mulDivUp(WAD, buyerPrice) : targetBuyerAssets.mulDivDown(WAD, buyerPrice);
    }
```

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** src/Midnight.sol (L361-364)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/periphery/MidnightBundles.sol (L197-221)
```text
        pullToken(loanToken, msg.sender, targetBuyerAssets, loanTokenPermit);
        forceApproveMax(loanToken, MIDNIGHT);

        uint256 referralFeeAssets = targetBuyerAssets.mulDivDown(referralFeePct, WAD);
        uint256 targetFilledBuyerAssets = targetBuyerAssets - referralFeeAssets;

        uint256 filledUnits;
        uint256 filledBuyerAssets;
        for (uint256 i; i < takes.length && filledBuyerAssets < targetFilledBuyerAssets; i++) {
            require(!takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
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

**File:** certora/specs/NoDivisionByZero.spec (L1-12)
```text
// SPDX-License-Identifier: GPL-2.0-or-later

// Proves that no division by zero occurs in mulDivDown or mulDivUp.
//
// All other Solidity divisions in the codebase use non-zero denominators:
// - settlementFee: divides by (end - start), always a positive constant from the breakpoint table.
// - setMarketSettlementFee / setDefaultSettlementFee: divide by CBP (1e12).
// - liquidate: divides by TIME_TO_MAX_LIF (15 minutes = 900).
// - tickToPrice: divides by 5e12 or a value greater than 1e18.
// - wExp, used in tickToPrice: divides by non-zero constants.
// Therefore, we only look for division by zero in mulDivDown and mulDivUp in this file.

```

**File:** test/MidnightBundlesTest.sol (L1084-1086)
```text
        for (uint256 i; i <= 6; i++) {
            midnight.setMarketSettlementFee(id, i, 0);
        }
```
