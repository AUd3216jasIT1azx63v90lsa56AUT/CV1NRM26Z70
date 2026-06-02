Audit Report

## Title
`buyerAssetsToUnits` unconditionally reverts with `PriceGreaterThanOne` for sell offers at `MAX_TICK` with non-zero `settlementFee`, causing `buyWithAssetsTargetAndWithdrawCollateral` to revert - (File: `src/periphery/TakeAmountsLib.sol`)

## Summary

`TakeAmountsLib.buyerAssetsToUnits` applies `require(buyerPrice <= WAD)` unconditionally for both buy and sell offers. For a sell offer at `tick = MAX_TICK`, `tickToPrice(MAX_TICK)` evaluates to exactly `WAD`, so `buyerPrice = WAD + settlementFee > WAD` whenever `settlementFee > 0`, triggering the revert. `MidnightBundles.buyWithAssetsTargetAndWithdrawCollateral` calls `buyerAssetsToUnits` outside any `try/catch`, so the revert propagates and the entire bundler transaction fails for any sell offer at `MAX_TICK` in a market with a non-zero settlement fee.

## Finding Description

**Root cause — `tickToPrice(MAX_TICK) = WAD`:**

In `TickLib.tickToPrice`, at `tick = MAX_TICK = 5820`:
- `wExp(LN_ONE_PLUS_DELTA * (2910 − 5820))` is `wExp(large_negative)` = `1e36 / wExp(large_positive)` = `0` (integer truncation).
- Denominator = `1e18 + 0 = 1e18`.
- `divHalfDownUnchecked(1e36, 1e18) = 1e18 = WAD`.
- After `PRICE_ROUNDING_STEP` rounding: still `WAD`.

This is implicitly confirmed by the test suite, which computes `TickLib.tickToPrice(MAX_TICK) + settlementFee` as `buyerPrice` in the `testSnappedBuyerAssets*` tests, treating `tickToPrice(MAX_TICK) = WAD` as a given. [1](#0-0) 

**Unconditional guard in `buyerAssetsToUnits`:**

For a sell offer (`offer.buy = false`), `sellerPrice = offerPrice = WAD` and `buyerPrice = WAD + settlementFee`. The guard at line 28 fires unconditionally:

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice; // = WAD
uint256 buyerPrice = sellerPrice + settlementFee;                           // = WAD + fee
require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne());                  // ← REVERTS
```

The NatSpec at line 14 documents this as intentional for the library: *"Reverts if buyerPrice > WAD, because not all buyerAssets are reachable then."* The library is correctly self-documenting its limitation, but the bundler does not account for it. [2](#0-1) 

**Bundler propagation — no `try/catch` around `buyerAssetsToUnits`:**

`buyWithAssetsTargetAndWithdrawCollateral` calls `buyerAssetsToUnits` at line 209 inside the loop, but the `try/catch` at line 215 only wraps `IMidnight(MIDNIGHT).take(...)`, not the `buyerAssetsToUnits` call. The NatSpec at line 175 explicitly documents: *"Reverts if TakeAmountsLib or ConsumableUnitsLib reverts."* The revert propagates to the top-level call, unwinding the entire transaction including the `pullToken` at line 197. [3](#0-2) [4](#0-3) 

**`Midnight.take` has no equivalent guard:**

`Midnight.take` computes `buyerPrice = sellerPrice + _settlementFee` and uses it directly in `units.mulDivUp(buyerPrice, WAD)` without any `<= WAD` check, confirming the divergence is specific to the periphery library. [5](#0-4) 

**Test suite independently confirms the gap:**

`TakeAmountsTest._maxTick` (lines 69–74) explicitly computes the highest tick where `tickToPrice(tick) + settlementFee <= WAD`. All four fuzz tests (lines 99, 121, 145, 168) bound `tick` strictly below `MAX_TICK` when `settlementFee > 0`. [6](#0-5) [7](#0-6) 

The `testSnappedBuyerAssets*` tests (lines 185–227) handle `MAX_TICK` by computing `targetUnits` directly and calling `take` directly — deliberately bypassing `buyerAssetsToUnits`. The bundler test `testBuyBuyerAssetsTarget` (line 189) resets all settlement fees to 0 before testing at `MAX_TICK`, explicitly avoiding the bug condition. [8](#0-7) [9](#0-8) 

## Impact Explanation

`MidnightBundles.buyWithAssetsTargetAndWithdrawCollateral` is completely unusable for any sell offer at `MAX_TICK` in a market where `settlementFee > 0` at the time of the call. The entire transaction reverts; because `pullToken` (line 197) is part of the same transaction, the user's tokens are returned by the EVM revert — no funds are permanently frozen. The concrete impact is a **DoS of the bundler function** for this valid offer configuration: the user cannot use the assets-target bundler path and must fall back to calling `Midnight.take` directly with a manually computed `units` value. [10](#0-9) 

## Likelihood Explanation

Three preconditions are required, all routine:
1. `settlementFeeCbp0 > 0` (or any non-zero settlement fee at the relevant time-to-maturity): Any value from `1 * CBP` to `MAX_SETTLEMENT_FEE_0_DAYS = 0.000014e18` triggers the bug. This is a normal protocol configuration.
2. `tick = MAX_TICK = 5820`: A valid tick (multiple of `DEFAULT_TICK_SPACING = 4`). Makers offering at the highest price naturally place offers here.
3. `settlementFee(id, timeToMaturity) > 0` at call time: Triggered post-maturity when `settlementFeeCbp0 > 0`, or pre-maturity when the interpolated fee at the current TTM is non-zero.

All three conditions can coexist in production without any privileged action. The bug is repeatable for every call to `buyWithAssetsTargetAndWithdrawCollateral` that includes a sell offer at `MAX_TICK` under these conditions. [11](#0-10) [12](#0-11) 

## Recommendation

The fix should be applied in `MidnightBundles.buyWithAssetsTargetAndWithdrawCollateral`. When `buyerPrice > WAD`, exact `targetBuyerAssets` values are not reachable (as the library documents), but the offer is still valid and `Midnight.take` accepts it. The bundler should handle this case by either:

1. **Wrapping `buyerAssetsToUnits` in a `try/catch`** and falling back to a units-based calculation (e.g., `targetBuyerAssets.mulDivDown(WAD, buyerPrice)`) when it reverts with `PriceGreaterThanOne`.
2. **Pre-checking `buyerPrice <= WAD`** before calling `buyerAssetsToUnits`, and using an alternative units computation for the `buyerPrice > WAD` case.
3. **Modifying `buyerAssetsToUnits`** to return a best-effort approximation (e.g., `targetBuyerAssets.mulDivDown(WAD, buyerPrice)`) instead of reverting when `buyerPrice > WAD`, with updated NatSpec.

Option 1 or 2 is preferred to avoid changing the library's documented invariant.

## Proof of Concept

Minimal Foundry test (extend `MidnightBundlesTest`):

```solidity
function testBuyAssetsTargetRevertsAtMaxTickWithFee() public {
    // Set a non-zero post-maturity settlement fee (index 0).
    midnight.setMarketSettlementFee(id, 0, 1e12); // 1 CBP

    offers[0].buy = false;
    offers[0].maker = borrower;
    offers[0].receiverIfMakerIsSeller = borrower;
    offers[0].tick = MAX_TICK; // 5820
    offers[0].maxUnits = 1000;

    collateralize(market, borrower, 1000);

    // Warp past maturity so settlementFee(id, 0) = 1e12 > 0.
    vm.warp(market.maturity + 1);

    Take[] memory takes = new Take[](1);
    takes[0] = Take({offer: offers[0], units: 1000, ratifierData: hex""});

    uint256 targetBuyerAssets = 1000; // any non-zero value

    vm.prank(lender);
    // Reverts with PriceGreaterThanOne instead of OutOfOffers or succeeding.
    vm.expectRevert(TickLib.PriceGreaterThanOne.selector);
    midnightBundles.buyWithAssetsTargetAndWithdrawCollateral(
        targetBuyerAssets, 0, lender, _noPermit(), takes,
        new CollateralWithdrawal[](0), address(0), 0, address(0)
    );
}
``` [13](#0-12) [14](#0-13)

### Citations

**File:** src/libraries/TickLib.sol (L6-6)
```text
uint256 constant MAX_TICK = 5820;
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

**File:** src/periphery/TakeAmountsLib.sol (L14-29)
```text
    /// @dev Reverts if buyerPrice > WAD, because not all buyerAssets are reachable then.
    /// @dev Reverts if offerPrice < settlementFee in case of a buy offer (midnight reverts too).
    /// @dev Returns a number of units for which take yields exactly targetBuyerAssets (not necessarily the biggest).
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
```

**File:** src/periphery/MidnightBundles.sol (L175-175)
```text
    /// @dev Reverts if TakeAmountsLib or ConsumableUnitsLib reverts.
```

**File:** src/periphery/MidnightBundles.sol (L197-197)
```text
        pullToken(loanToken, msg.sender, targetBuyerAssets, loanTokenPermit);
```

**File:** src/periphery/MidnightBundles.sol (L205-221)
```text
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

**File:** src/Midnight.sol (L361-364)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** test/TakeAmountsTest.sol (L69-74)
```text
    /// @dev Returns the highest tick such that tickToPrice(tick) + settlementFee <= WAD.
    function _maxTick(uint256 settlementFee) internal pure returns (uint256) {
        uint256 maxPrice = WAD - settlementFee;
        uint256 t = TickLib.priceToTick(maxPrice, 1);
        return TickLib.tickToPrice(t) > maxPrice ? t - 1 : t;
    }
```

**File:** test/TakeAmountsTest.sol (L97-99)
```text
        uint256 settlementFee = _setSettlementFees(settlementFee0, settlementFee1);
        targetBuyerAssets = bound(targetBuyerAssets, 1, 1e30);
        tick = bound(tick, 4, _maxTick(settlementFee) / DEFAULT_TICK_SPACING) * DEFAULT_TICK_SPACING;
```

**File:** test/TakeAmountsTest.sol (L185-205)
```text
    function testSnappedBuyerAssetsBuyerIsLender(
        uint256 targetBuyerAssets,
        uint256 settlementFee0,
        uint256 settlementFee1
    ) public {
        uint256 settlementFee = _setSettlementFees(settlementFee0, settlementFee1);
        targetBuyerAssets = bound(targetBuyerAssets, 1, 1e30);

        uint256 buyerPrice = TickLib.tickToPrice(MAX_TICK) + settlementFee;
        uint256 targetUnits = targetBuyerAssets.mulDivUp(WAD, buyerPrice);

        deal(address(loanToken), lender, type(uint256).max);
        collateralize(market, borrower, targetUnits);
        offer.maker = borrower;
        offer.receiverIfMakerIsSeller = borrower;
        offer.tick = MAX_TICK;

        (uint256 buyerAssets,) = take(targetUnits, lender, offer);

        assertEq(buyerAssets, targetBuyerAssets.mulDivUp(WAD, buyerPrice).mulDivUp(buyerPrice, WAD), "e2e buyerAssets");
    }
```

**File:** test/MidnightBundlesTest.sol (L200-204)
```text

        // Reset settlement fees so buyerPrice = price <= WAD at MAX_TICK.
        for (uint256 i; i <= 6; i++) {
            midnight.setMarketSettlementFee(id, i, 0);
        }
```

**File:** src/libraries/ConstantsLib.sol (L11-11)
```text
uint256 constant MAX_SETTLEMENT_FEE_0_DAYS = 0.000014e18;
```
