Audit Report

## Title
Division by Zero in `sellerAssetsToUnits` When `sellerPrice == 0` Causes Unexpected Revert in `supplyCollateralAndSellWithAssetsTarget` - (File: src/periphery/TakeAmountsLib.sol)

## Summary
`TakeAmountsLib.sellerAssetsToUnits` divides by `sellerPrice` with no guard for the zero case. When a buy offer's `tickToPrice(tick) == settlementFee`, `sellerPrice` is zero and the call panics with a Solidity 0.8 division-by-zero. Because this call sits outside the `try/catch` block in `MidnightBundles.supplyCollateralAndSellWithAssetsTarget`, the panic propagates and reverts the entire bundler transaction — even though `Midnight.take()` itself would succeed for that offer (yielding `sellerAssets = 0`).

## Finding Description

**Root cause — `TakeAmountsLib.sellerAssetsToUnits` (lines 44–46):**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...;
```

When `offer.buy == true` and `offerPrice == settlementFee`, `sellerPrice = 0`. The subsequent `mulDivUp(targetSellerAssets, WAD, 0)` is a Solidity 0.8 division-by-zero panic. The NatSpec at line 34 documents only the `offerPrice < settlementFee` revert case; the equal case is undocumented and unguarded. [1](#0-0) 

**Why `Midnight.take()` does NOT revert in this case:**

In `Midnight.take()`, `sellerAssets = units.mulDivDown(sellerPrice, WAD)`. When `sellerPrice == 0`, this evaluates to `0` — multiplication, not division, so no panic. The take succeeds with `sellerAssets = 0`. The asymmetry is that `sellerAssetsToUnits` divides *by* `sellerPrice`, while `Midnight.take()` multiplies *by* it. [2](#0-1) 

**Contrast with `buyerAssetsToUnits`:**

`buyerAssetsToUnits` computes `buyerPrice = sellerPrice + settlementFee`. When `sellerPrice == 0`, `buyerPrice = settlementFee > 0`, so no division by zero occurs. The asymmetry is the root cause. [3](#0-2) 

**Where the panic propagates — `MidnightBundles.supplyCollateralAndSellWithAssetsTarget` (lines 285–291):**

```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(   // <-- NOT in try/catch
        MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
    ),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
);
try IMidnight(MIDNIGHT).take(...) ...   // only this is guarded
```

The `sellerAssetsToUnits` call is outside the `try/catch`. The panic bypasses the catch block and reverts the entire transaction, including collateral supply and all prior takes. The NatSpec on line 246 explicitly acknowledges "Reverts if TakeAmountsLib or ConsumableUnitsLib reverts," confirming this is an unintended divergence from the asynchrony-handling design intent. [4](#0-3) 

**Asynchrony trigger path:**

The settlement fee is a function of time-to-maturity. Between transaction construction and execution, time elapses and the fee value changes. An offer that had `sellerPrice > 0` at construction time can have `sellerPrice == 0` at execution time. The contract is explicitly designed to handle asynchrony by catching reverts from `take()`, but the `sellerAssetsToUnits` panic escapes this guard.

**`tickToPrice(0) == 0` confirms the zero-price case is reachable:** [5](#0-4) 

Tick prices and settlement fees are both rounded to multiples of `PRICE_ROUNDING_STEP` (1e12), making exact coincidence structurally possible across the tick range.

## Impact Explanation
`MidnightBundles.supplyCollateralAndSellWithAssetsTarget` reverts with a division-by-zero panic whenever any offer in the `takes` array satisfies `tickToPrice(offer.tick) == settlementFee` and `offer.buy == true` at execution time. The taker's entire transaction fails — collateral supply, all takes, and the loan asset transfer — even though `Midnight.take()` itself would succeed for that offer (yielding `sellerAssets = 0`). This is a concrete, reproducible DoS on the bundler's sell-with-assets-target flow, causing loss of gas and failed atomic execution for affected takers.

## Likelihood Explanation
The settlement fee is a piecewise-linear function of time-to-maturity that changes continuously as time elapses. Tick prices are discrete values rounded to multiples of 1e12; settlement fees are also rounded to multiples of 1e12. Coincidence is structurally plausible. More critically, a maker can deliberately select a tick whose price matches the current settlement fee — no privileged action is required. The condition is repeatable: any taker who includes such an offer in `supplyCollateralAndSellWithAssetsTarget` with `targetSellerAssets > 0` triggers the revert. The asynchrony window (fee changing between construction and execution) makes this reachable even for takers acting in good faith.

## Recommendation
Add a guard in `sellerAssetsToUnits` for the `sellerPrice == 0` case. When `sellerPrice == 0`, any nonzero `targetSellerAssets` is unreachable (since `sellerAssets = units * 0 / WAD = 0` for all units), so the function should return `type(uint256).max` (signaling "infinite units needed") or revert with a clean, catchable error rather than panicking. Alternatively, `supplyCollateralAndSellWithAssetsTarget` could wrap the `sellerAssetsToUnits` call in a `try/catch` or check `sellerPrice == 0` before calling it and skip the offer.

```solidity
function sellerAssetsToUnits(...) internal view returns (uint256) {
    uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
    if (offer.buy && sellerPrice == 0) return type(uint256).max; // unreachable target
    return offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...;
}
```

## Proof of Concept
1. Deploy `Midnight` and `MidnightBundles` on a local fork.
2. Set the market settlement fee to a value equal to `tickToPrice(T)` for some tick `T` that is a multiple of the market's tick spacing (e.g., set `settlementFee = tickToPrice(2) = 1e12`).
3. Have a maker place a buy offer at tick `T` (so `offerPrice == settlementFee`).
4. Have a taker call `supplyCollateralAndSellWithAssetsTarget` with `targetSellerAssets > 0` and `takes[0]` pointing to that offer.
5. Observe: the transaction reverts with a Solidity panic (division by zero) from `sellerAssetsToUnits`, not from `Midnight.take()`.
6. Confirm: calling `Midnight.take()` directly with the same offer and any nonzero `units` succeeds and returns `sellerAssets = 0`.

### Citations

**File:** src/periphery/TakeAmountsLib.sol (L26-29)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + settlementFee;
        require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne());
        return offer.buy ? targetBuyerAssets.mulDivUp(WAD, buyerPrice) : targetBuyerAssets.mulDivDown(WAD, buyerPrice);
```

**File:** src/periphery/TakeAmountsLib.sol (L44-46)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        return
            offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

**File:** src/Midnight.sol (L361-364)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
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
