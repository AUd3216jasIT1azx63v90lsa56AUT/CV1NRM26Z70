Audit Report

## Title
`buyerAssetsToUnits` unconditionally reverts with `PriceGreaterThanOne` for sell offers at `MAX_TICK` with non-zero `settlementFee`, causing `buyWithAssetsTargetAndWithdrawCollateral` to revert - (File: `src/periphery/TakeAmountsLib.sol`)

## Summary

`TakeAmountsLib.buyerAssetsToUnits` applies `require(buyerPrice <= WAD)` unconditionally. For a sell offer at `tick = MAX_TICK`, `tickToPrice(MAX_TICK)` evaluates to exactly `WAD`, so `buyerPrice = WAD + settlementFee > WAD` whenever `settlementFee > 0`, triggering the revert. `MidnightBundles.buyWithAssetsTargetAndWithdrawCollateral` calls `buyerAssetsToUnits` outside any `try/catch`, so the revert propagates and the entire bundler transaction fails for any sell offer at `MAX_TICK` in a market with a non-zero settlement fee.

## Finding Description

**Root cause — `tickToPrice(MAX_TICK) = WAD`:**

In `TickLib.tickToPrice`, at `tick = MAX_TICK = 5820`:

```solidity
return uint256(1e36)
    .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
    .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
```

`int256(MAX_TICK / 2) - int256(MAX_TICK) = 2910 - 5820 = -2910`. `wExp` of a large negative value returns `1e36 / wExp(large_positive)`, which truncates to `0`. The denominator becomes `1e18 + 0 = 1e18`, and `divHalfDownUnchecked(1e36, 1e18) = 1e18 = WAD`. After `PRICE_ROUNDING_STEP` rounding, the result is still `WAD`. [1](#0-0) 

This is independently confirmed by the test suite, which computes `TickLib.tickToPrice(MAX_TICK) + settlementFee` as `buyerPrice` directly in the `testSnappedBuyerAssets*` tests, treating `tickToPrice(MAX_TICK) = WAD` as a given. [2](#0-1) 

**Unconditional guard in `buyerAssetsToUnits`:**

For a sell offer (`offer.buy = false`), `sellerPrice = offerPrice = WAD` and `buyerPrice = WAD + settlementFee`. The guard at line 28 fires unconditionally:

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice; // = WAD
uint256 buyerPrice = sellerPrice + settlementFee;                           // = WAD + fee
require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne());                  // ← REVERTS
``` [3](#0-2) 

The NatSpec at line 14 documents this as intentional for the library: *"Reverts if buyerPrice > WAD, because not all buyerAssets are reachable then."* The library is correctly self-documenting its limitation, but the bundler does not account for it. [4](#0-3) 

**Bundler propagation — no `try/catch` around `buyerAssetsToUnits`:**

`buyWithAssetsTargetAndWithdrawCollateral` calls `buyerAssetsToUnits` at line 209 inside the loop, but the `try/catch` at line 215 only wraps `IMidnight(MIDNIGHT).take(...)`, not the `buyerAssetsToUnits` call: [5](#0-4) 

The NatSpec at line 175 explicitly documents: *"Reverts if TakeAmountsLib or ConsumableUnitsLib reverts."* The revert propagates to the top-level call, unwinding the entire transaction including the `pullToken` at line 197. [6](#0-5) [7](#0-6) 

**`Midnight.take` has no equivalent guard:**

`Midnight.take` computes `buyerPrice = sellerPrice + _settlementFee` and uses it directly in `units.mulDivUp(buyerPrice, WAD)` without any `<= WAD` check, confirming the divergence is specific to the periphery library. [8](#0-7) 

**Test suite independently confirms the gap:**

`TakeAmountsTest._maxTick` (lines 70–74) explicitly computes the highest tick where `tickToPrice(tick) + settlementFee <= WAD`. All four fuzz tests bound `tick` strictly below `MAX_TICK` when `settlementFee > 0`. [9](#0-8) [10](#0-9) 

The `testSnappedBuyerAssets*` tests handle `MAX_TICK` by computing `targetUnits` directly and calling `take` directly — deliberately bypassing `buyerAssetsToUnits`. [11](#0-10) 

## Impact Explanation

`MidnightBundles.buyWithAssetsTargetAndWithdrawCollateral` is completely unusable for any sell offer at `MAX_TICK` in a market where `settlementFee > 0` at the time of the call. The entire transaction reverts; because `pullToken` (line 197) is part of the same transaction, the user's tokens are returned by the EVM revert — no funds are permanently frozen. The concrete impact is a **DoS of the bundler function** for this valid offer configuration: the user cannot use the assets-target bundler path and must fall back to calling `Midnight.take` directly with a manually computed `units` value. [7](#0-6) 

## Likelihood Explanation

Three preconditions are required, all routine:
1. `settlementFeeCbp0 > 0` (or any non-zero settlement fee at the relevant time-to-maturity): Any value from `1 * CBP` to `MAX_SETTLEMENT_FEE_0_DAYS` triggers the bug. This is a normal protocol configuration.
2. `tick = MAX_TICK = 5820`: A valid tick (multiple of `DEFAULT_TICK_SPACING = 4`, since `5820 / 4 = 1455`). Makers offering at the highest price naturally place offers here.
3. `settlementFee(id, timeToMaturity) > 0` at call time: Triggered post-maturity when `settlementFeeCbp0 > 0`, or pre-maturity when the interpolated fee at the current TTM is non-zero.

All three conditions can coexist in production without any privileged action. The bug is repeatable for every call to `buyWithAssetsTargetAndWithdrawCollateral` that includes a sell offer at `MAX_TICK` under these conditions. [12](#0-11) [13](#0-12) 

## Recommendation

In `MidnightBundles.buyWithAssetsTargetAndWithdrawCollateral`, wrap the `TakeAmountsLib.buyerAssetsToUnits` call in a `try/catch` (or a pre-check) so that offers where `buyerPrice > WAD` are skipped rather than reverting the entire transaction — consistent with how `IMidnight.take` failures are already handled. Alternatively, `TakeAmountsLib.buyerAssetsToUnits` could return `type(uint256).max` instead of reverting when `buyerPrice > WAD`, signaling to the caller that the offer cannot be expressed as an assets target. [5](#0-4) [14](#0-13) 

## Proof of Concept

Minimal Foundry test:

1. Deploy `Midnight` and `MidnightBundles`.
2. Create a market with `settlementFeeCbp0 > 0` (e.g., `1 * CBP`).
3. Create a sell offer at `tick = MAX_TICK = 5820`.
4. Call `buyWithAssetsTargetAndWithdrawCollateral` with that offer and any `targetBuyerAssets > 0`.
5. Observe revert with `PriceGreaterThanOne`.

The existing test `testSnappedBuyerAssetsBuyerIsLender` (lines 185–205 of `TakeAmountsTest.sol`) already demonstrates that `tickToPrice(MAX_TICK) + settlementFee > WAD` when `settlementFee > 0`, and deliberately avoids calling `buyerAssetsToUnits` for this case — confirming the gap. [11](#0-10)

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

**File:** test/TakeAmountsTest.sol (L69-74)
```text
    /// @dev Returns the highest tick such that tickToPrice(tick) + settlementFee <= WAD.
    function _maxTick(uint256 settlementFee) internal pure returns (uint256) {
        uint256 maxPrice = WAD - settlementFee;
        uint256 t = TickLib.priceToTick(maxPrice, 1);
        return TickLib.tickToPrice(t) > maxPrice ? t - 1 : t;
    }
```

**File:** test/TakeAmountsTest.sol (L99-99)
```text
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

**File:** src/periphery/TakeAmountsLib.sol (L14-14)
```text
    /// @dev Reverts if buyerPrice > WAD, because not all buyerAssets are reachable then.
```

**File:** src/periphery/TakeAmountsLib.sol (L26-29)
```text
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

**File:** src/Midnight.sol (L361-364)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/Midnight.sol (L963-979)
```text
    function settlementFee(bytes32 id, uint256 timeToMaturity) public view returns (uint256) {
        MarketState storage _marketState = marketState[id];
        require(_marketState.tickSpacing > 0, MarketNotCreated());

        if (timeToMaturity >= 360 days) return _marketState.settlementFeeCbp6 * CBP;

        // forgefmt: disable-start
        (uint256 start, uint256 end, uint256 feeLower, uint256 feeUpper) =
            timeToMaturity < 1 days   ? (  0 days,   1 days, _marketState.settlementFeeCbp0 * CBP, _marketState.settlementFeeCbp1 * CBP) :
            timeToMaturity < 7 days   ? (  1 days,   7 days, _marketState.settlementFeeCbp1 * CBP, _marketState.settlementFeeCbp2 * CBP) :
            timeToMaturity < 30 days  ? (  7 days,  30 days, _marketState.settlementFeeCbp2 * CBP, _marketState.settlementFeeCbp3 * CBP) :
            timeToMaturity < 90 days  ? ( 30 days,  90 days, _marketState.settlementFeeCbp3 * CBP, _marketState.settlementFeeCbp4 * CBP) :
            timeToMaturity < 180 days ? ( 90 days, 180 days, _marketState.settlementFeeCbp4 * CBP, _marketState.settlementFeeCbp5 * CBP) :
                                        (180 days, 360 days, _marketState.settlementFeeCbp5 * CBP, _marketState.settlementFeeCbp6 * CBP);
        // forgefmt: disable-end

        return (feeLower * (end - timeToMaturity) + feeUpper * (timeToMaturity - start)) / (end - start);
```
