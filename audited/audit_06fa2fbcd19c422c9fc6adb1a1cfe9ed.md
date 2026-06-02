Audit Report

## Title
`buyerAssetsToUnits` unconditionally reverts with `PriceGreaterThanOne` for sell offers at `MAX_TICK` with non-zero `settlementFee`, causing `buyWithAssetsTargetAndWithdrawCollateral` to DoS - (File: `src/periphery/TakeAmountsLib.sol`)

## Summary

`TakeAmountsLib.buyerAssetsToUnits` applies `require(buyerPrice <= WAD)` unconditionally for both buy and sell offers. For a sell offer at `tick = MAX_TICK`, `tickToPrice(MAX_TICK)` evaluates to exactly `WAD`, so `buyerPrice = WAD + settlementFee > WAD` whenever `settlementFee > 0`, triggering the revert. `Midnight.take` has no equivalent guard and executes successfully for the same inputs. Because `MidnightBundles.buyWithAssetsTargetAndWithdrawCollateral` calls `buyerAssetsToUnits` outside any `try/catch`, the entire bundler transaction reverts for any sell offer at `MAX_TICK` in a market with a non-zero settlement fee at or after maturity.

## Finding Description

**Root cause — unconditional guard in `buyerAssetsToUnits`:** [1](#0-0) 

For a sell offer (`offer.buy = false`), `sellerPrice = offerPrice` and `buyerPrice = offerPrice + settlementFee`. At `tick = MAX_TICK = 5820`, `tickToPrice` computes `wExp` of a large negative exponent, which integer-divides to 0, yielding `divHalfDownUnchecked(1e36, 1e18) = 1e18 = WAD`. After `PRICE_ROUNDING_STEP` rounding the result is still `WAD`. Therefore `buyerPrice = WAD + settlementFee > WAD` and the `require` at line 28 reverts unconditionally. [2](#0-1) 

**`Midnight.take` has no equivalent guard:**

The `testSnappedBuyerAssets*` tests confirm `take` executes successfully at `MAX_TICK` with non-zero `settlementFee` — they compute `targetUnits` directly and call `take` directly, deliberately bypassing `buyerAssetsToUnits`: [3](#0-2) 

**Bundler propagation — `buyWithAssetsTargetAndWithdrawCollateral`:**

`TakeAmountsLib.buyerAssetsToUnits` is called at line 209 outside any `try/catch`. The NatSpec at line 175 explicitly documents: *"Reverts if TakeAmountsLib or ConsumableUnitsLib reverts."* The revert propagates to the top-level call. [4](#0-3) [5](#0-4) 

**Test suite independently confirms the gap:**

`_maxTick(settlementFee)` computes the highest tick where `tickToPrice(tick) + settlementFee <= WAD`. All four fuzz tests bound `tick` strictly below this value when `settlementFee > 0`: [6](#0-5) [7](#0-6) 

## Impact Explanation

`MidnightBundles.buyWithAssetsTargetAndWithdrawCollateral` is completely unusable for any sell offer at `MAX_TICK` in a market where `settlementFeeCbp0 > 0` once `block.timestamp >= offer.market.maturity`. The entire transaction reverts; because `pullToken` (line 197) is part of the same transaction, the EVM revert returns the user's tokens — no funds are permanently frozen. The concrete impact is a **DoS of the bundler's assets-target buy path** for a valid, reachable market state. Users must fall back to calling `Midnight.take` directly with a manually computed `units` value. [8](#0-7) 

## Likelihood Explanation

All three preconditions are routine and can coexist in production:

1. **`settlementFeeCbp0 > 0`**: `MAX_SETTLEMENT_FEE_0_DAYS = 0.000014e18`; any value from `1 * CBP` to `14 * CBP` triggers the bug. [9](#0-8) 

2. **`tick = MAX_TICK = 5820`**: A valid tick (5820 is a multiple of `DEFAULT_TICK_SPACING = 4`). Makers offering at the highest price naturally place offers here. [10](#0-9) [11](#0-10) 

3. **`block.timestamp >= offer.market.maturity`**: Normal post-maturity state, reached by every market.

No attacker action is required — the revert is triggered by the user's own valid transaction.

## Recommendation

In `buyerAssetsToUnits`, apply the `require(buyerPrice <= WAD)` guard only for buy offers, mirroring the asymmetry already present in the price computation. For sell offers, `buyerPrice > WAD` is a valid state (the buyer pays more than 1:1 in assets per unit) and `take` handles it correctly. Alternatively, document that `buyerAssetsToUnits` is not supported for sell offers at `MAX_TICK` when `settlementFee > 0`, and guard against this case in `buyWithAssetsTargetAndWithdrawCollateral` by skipping or reverting with a descriptive error before calling `buyerAssetsToUnits`.

## Proof of Concept

1. Deploy a market with `settlementFeeCbp0 = 1 * CBP` (minimum non-zero value).
2. Advance `block.timestamp` to `>= market.maturity` (0-day fee applies).
3. Create a sell offer at `tick = MAX_TICK = 5820`.
4. Call `MidnightBundles.buyWithAssetsTargetAndWithdrawCollateral` with that offer and any `targetBuyerAssets > 0`.
5. Observe revert with `PriceGreaterThanOne`.
6. Confirm `Midnight.take` with the same offer and manually computed `units = targetBuyerAssets.mulDivUp(WAD, WAD + settlementFee)` succeeds. [12](#0-11)

### Citations

**File:** src/periphery/TakeAmountsLib.sol (L26-28)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + settlementFee;
        require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne());
```

**File:** src/libraries/TickLib.sol (L6-6)
```text
uint256 constant MAX_TICK = 5820;
```

**File:** src/libraries/TickLib.sol (L44-51)
```text
    function tickToPrice(uint256 tick) internal pure returns (uint256) {
        require(tick <= MAX_TICK, TickOutOfRange());
        unchecked {
            // forge-lint: disable-next-item(unsafe-typecast)
            return uint256(1e36)
                    .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
                    .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
        }
```

**File:** test/TakeAmountsTest.sol (L70-74)
```text
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

**File:** src/periphery/MidnightBundles.sol (L175-175)
```text
    /// @dev Reverts if TakeAmountsLib or ConsumableUnitsLib reverts.
```

**File:** src/periphery/MidnightBundles.sol (L197-197)
```text
        pullToken(loanToken, msg.sender, targetBuyerAssets, loanTokenPermit);
```

**File:** src/periphery/MidnightBundles.sol (L208-214)
```text
            uint256 unitsToTake = min(
                TakeAmountsLib.buyerAssetsToUnits(
                    MIDNIGHT, id, takes[i].offer, targetFilledBuyerAssets - filledBuyerAssets
                ),
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
```

**File:** src/libraries/ConstantsLib.sol (L11-11)
```text
uint256 constant MAX_SETTLEMENT_FEE_0_DAYS = 0.000014e18;
```

**File:** src/libraries/ConstantsLib.sol (L26-26)
```text
uint8 constant DEFAULT_TICK_SPACING = 4;
```
