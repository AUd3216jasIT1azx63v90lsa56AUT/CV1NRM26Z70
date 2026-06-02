Audit Report

## Title
Division-by-Zero in `buyerAssetsToUnits` on Zero-Price Sell Offer Causes Bundler DoS - (File: src/periphery/TakeAmountsLib.sol)

## Summary
When a sell offer has `tick=0` and the market's `settlementFee=0`, `TickLib.tickToPrice(0)` returns `0`, making `buyerPrice=0`. `buyerAssetsToUnits` then executes `targetBuyerAssets.mulDivDown(WAD, 0)`, which is `(targetBuyerAssets * WAD) / 0` and panics. Because the `buyerAssetsToUnits` call in `buyWithAssetsTargetAndWithdrawCollateral` sits outside the `try/catch` block, the entire bundler transaction reverts unconditionally.

## Finding Description
`TickLib.tickToPrice(0)` returns `0`, confirmed by the unit test and the Certora spec rule `tickToPriceIsZeroAtZero`. [1](#0-0) [2](#0-1) 

In `buyerAssetsToUnits`, with `offer.buy=false`, `tick=0`, and `settlementFee=0`:
- `offerPrice = TickLib.tickToPrice(0) = 0`
- `sellerPrice = offerPrice = 0` (sell-offer branch)
- `buyerPrice = 0 + 0 = 0`
- `require(0 <= WAD)` passes
- `return targetBuyerAssets.mulDivDown(WAD, 0)` → `(targetBuyerAssets * WAD) / 0` → Solidity 0.8 division-by-zero panic [3](#0-2) 

`mulDivDown` is a plain `(x * y) / d` with no `unchecked` wrapper, so `d=0` panics unconditionally. [4](#0-3) 

This is confirmed by the existing test `testMulDivDownDivisionByZero` which expects `stdError.divisionError` when `d=0`. [5](#0-4) 

In `buyWithAssetsTargetAndWithdrawCollateral`, the `buyerAssetsToUnits` call at lines 208-211 is evaluated as an argument to `min(...)` **before** the `try` at line 215. A revert inside `buyerAssetsToUnits` is not caught and propagates to the caller. [6](#0-5) 

The `NoDivisionByZero` Certora spec only verifies `Midnight.sol` methods and explicitly does not cover `TakeAmountsLib`. [7](#0-6) 

The existing `TakeAmountsTest` fuzz tests bound `tick` away from zero (`tick = bound(tick, 4, ...)`), so this path is never exercised. [8](#0-7) 

## Impact Explanation
Any call to `buyWithAssetsTargetAndWithdrawCollateral` that includes a sell offer with `tick=0` and a market where `settlementFee=0` will revert unconditionally when `targetBuyerAssets > 0`. The bundler's asset-denominated targeting functionality is completely unavailable for zero-price offers. Funds are not permanently frozen (the `pullToken` is unwound on revert), so the impact is a targeted DoS of the periphery contract. [9](#0-8) 

## Likelihood Explanation
The preconditions are minimal and fully attacker-controllable. Any unprivileged user can post a sell offer with `tick=0` — there is no validation preventing it. Settlement fee is a market-level parameter; the bundler tests explicitly set it to `0` for multiple breakpoints, confirming this is a common and expected market configuration. [10](#0-9) 

The condition is stable and repeatable: every call to `buyWithAssetsTargetAndWithdrawCollateral` targeting such an offer will revert.

## Recommendation
Guard against `buyerPrice == 0` in `buyerAssetsToUnits`. The simplest fix is to add a `require(buyerPrice > 0)` check before the `mulDivDown` call, or to return `0` units when `buyerPrice == 0` (since a zero-price offer yields zero buyer assets per unit, making the asset-denominated target unreachable). The same guard should be applied to `sellerAssetsToUnits` for the `sellerPrice == 0` case. [11](#0-10) 

## Proof of Concept
Minimal Foundry test:
1. Deploy `Midnight` and `MidnightBundles`.
2. Set `settlementFee` to `0` for all breakpoints on a market.
3. Have an unprivileged borrower post a sell offer with `tick=0`.
4. Call `buyWithAssetsTargetAndWithdrawCollateral` with `targetBuyerAssets=1` targeting that offer.
5. Observe the transaction reverts with `Panic(0x12)` (division by zero) rather than `OutOfOffers` or a successful fill.

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

**File:** src/periphery/TakeAmountsLib.sol (L22-29)
```text
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
        uint256 settlementFee =
            IMidnight(midnight).settlementFee(id, UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp));
        // Mirrors Midnight's computation to revert if offerPrice < settlementFee in case of a buy offer.
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + settlementFee;
        require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne());
        return offer.buy ? targetBuyerAssets.mulDivUp(WAD, buyerPrice) : targetBuyerAssets.mulDivDown(WAD, buyerPrice);
```

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** test/UtilsLibTest.sol (L58-63)
```text
    function testMulDivDownDivisionByZero(uint256 x, uint256 y) public {
        if (x > 0) y = bound(y, 0, type(uint256).max / x);

        vm.expectRevert(stdError.divisionError);
        this.mulDivDown(x, y, 0);
    }
```

**File:** src/periphery/MidnightBundles.sol (L197-197)
```text
        pullToken(loanToken, msg.sender, targetBuyerAssets, loanTokenPermit);
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

**File:** certora/specs/NoDivisionByZero.spec (L109-112)
```text
rule noDivisionByZero(method f, env e, calldataarg args) filtered { f -> f.selector != sig:liquidate(Midnight.Market, uint256, uint256, uint256, address, bool, address, address, bytes).selector } {
    f(e, args);
    assert true;
}
```

**File:** test/TakeAmountsTest.sol (L99-99)
```text
        tick = bound(tick, 4, _maxTick(settlementFee) / DEFAULT_TICK_SPACING) * DEFAULT_TICK_SPACING;
```

**File:** test/MidnightBundlesTest.sol (L201-204)
```text
        // Reset settlement fees so buyerPrice = price <= WAD at MAX_TICK.
        for (uint256 i; i <= 6; i++) {
            midnight.setMarketSettlementFee(id, i, 0);
        }
```
