Audit Report

## Title
`buyWithAssetsTargetAndWithdrawCollateral` permanently reverts for sell offers near `MAX_TICK` when settlement fee is non-zero - (`File: src/periphery/MidnightBundles.sol`)

## Summary

`TakeAmountsLib.buyerAssetsToUnits` enforces `require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne())` at line 28, which reverts when `offerPrice + settlementFee > WAD` for sell offers. This call is placed outside the try/catch block in `buyWithAssetsTargetAndWithdrawCollateral` (lines 209–211), causing the entire bundle transaction to revert rather than skipping the offer. The core `Midnight.take()` performs the identical price computation with no such guard and would succeed for the same offer.

## Finding Description

**Root cause — `TakeAmountsLib.buyerAssetsToUnits` (`src/periphery/TakeAmountsLib.sol`):**

For a sell offer (`offer.buy = false`):
- Line 26: `sellerPrice = offerPrice`
- Line 27: `buyerPrice = offerPrice + settlementFee`
- Line 28: `require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne())` — unconditionally reverts when `offerPrice + settlementFee > WAD` [1](#0-0) 

**Asymmetry with `Midnight.take()` (`src/Midnight.sol`):**

Lines 361–363 perform the identical computation (`sellerPrice = offerPrice`, `buyerPrice = sellerPrice + settlementFee`) but proceed directly to `mulDivUp` with no `require(buyerPrice <= WAD)` guard. The core accepts these offers; the periphery does not. [2](#0-1) 

**Call placement in `buyWithAssetsTargetAndWithdrawCollateral` (`src/periphery/MidnightBundles.sol`):**

The `TakeAmountsLib.buyerAssetsToUnits` call at lines 209–211 is computed as the first argument to `min(...)`, which is evaluated before the `try` block at line 215. If `buyerAssetsToUnits` reverts, execution never reaches the try/catch, and the entire transaction reverts. [3](#0-2) 

The natspec at line 175 explicitly acknowledges this: *"Reverts if TakeAmountsLib or ConsumableUnitsLib reverts."* [4](#0-3) 

Note: for sell offers, `ConsumableUnitsLib.consumableUnits` (line 213) calls `sellerAssetsToUnits` (not `buyerAssetsToUnits`), so it does not independently trigger the same revert — but the primary revert from line 209 is sufficient to DoS the function. [5](#0-4) 

**Why existing guards are insufficient:**

The try/catch at lines 215–221 only wraps `IMidnight(MIDNIGHT).take(...)`. Pre-computation calls outside this block propagate reverts unconditionally. There is no fallback, skip, or catch mechanism for `TakeAmountsLib` failures. [6](#0-5) 

## Impact Explanation

Any taker calling `buyWithAssetsTargetAndWithdrawCollateral` with a sell offer at tick T where `tickToPrice(T) + settlementFee > WAD` will have the entire bundle transaction revert unconditionally. The taker cannot use this periphery function for those offers at all, even though `Midnight.take()` would accept and settle them. This is a permanent, unconditional DoS of the periphery buy-with-assets-target path for all sell offers near `MAX_TICK` whenever the market settlement fee is non-zero.

## Likelihood Explanation

All three preconditions are routine and require no privileged action:
1. A market with a non-zero settlement fee at the current TTM breakpoint — standard governance configuration.
2. A maker places a sell offer at `MAX_TICK` (or any tick T where `tickToPrice(T) + settlementFee > WAD`). With any non-zero fee, `MAX_TICK` satisfies this.
3. A taker calls `buyWithAssetsTargetAndWithdrawCollateral` with that offer in `takes[]`.

The DoS is deterministic and repeatable: every call with such an offer reverts. No victim mistake or external dependency is required.

## Recommendation

Move the `TakeAmountsLib.buyerAssetsToUnits` and `ConsumableUnitsLib.consumableUnits` calls inside the try/catch block, or wrap them in a separate try/catch that skips the offer on revert (consistent with the documented intent of skipping offers that would cause `take()` to revert). Alternatively, remove the `require(buyerPrice <= WAD)` guard from `buyerAssetsToUnits` for sell offers and handle the arithmetic directly, mirroring the core's behavior.

## Proof of Concept

1. Deploy Midnight with a market where `settlementFee > 0` at the current TTM breakpoint.
2. Have a maker place a sell offer at `MAX_TICK` (so `tickToPrice(MAX_TICK) + settlementFee > WAD`).
3. Have a taker call `buyWithAssetsTargetAndWithdrawCollateral` with that offer in `takes[]`.
4. Observe the transaction reverts with `PriceGreaterThanOne` from `TakeAmountsLib.buyerAssetsToUnits` at line 28 of `TakeAmountsLib.sol`.
5. Confirm that calling `Midnight.take()` directly with the same offer and units succeeds. [7](#0-6)

### Citations

**File:** src/periphery/TakeAmountsLib.sol (L26-28)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + settlementFee;
        require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne());
```

**File:** src/Midnight.sol (L361-363)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
```

**File:** src/periphery/MidnightBundles.sol (L175-175)
```text
    /// @dev Reverts if TakeAmountsLib or ConsumableUnitsLib reverts.
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

**File:** src/periphery/ConsumableUnitsLib.sol (L20-21)
```text
        } else {
            return TakeAmountsLib.sellerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
```
