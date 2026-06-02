All four claims are verified against the actual source code:

**Claim 1** — `UtilsLib.mulDivUp` at line 35: [1](#0-0) 
`d - 1` underflows when `d = 0` under Solidity 0.8 checked arithmetic. ✓

**Claim 2** — `sellerAssetsToUnits` with `sellerPrice = 0`: [2](#0-1) 
When `offer.buy = true` and `offerPrice == settlementFee`, `sellerPrice = 0`, and `mulDivUp(WAD, 0)` is called → revert. ✓

**Claim 3** — `sellerAssetsToUnits` called outside `try/catch`: [3](#0-2) 
`sellerAssetsToUnits` is at line 286 (inside the `min(...)` argument), the `try` block starts at line 292. Revert propagates unconditionally. ✓

**Claim 4** — NatSpec discrepancy: [4](#0-3) 
The comment says "Reverts if offerPrice **<** settlementFee … (midnight reverts too)." At the `==` boundary, the periphery reverts (denominator 0 in `mulDivUp`) while `Midnight.take()` uses `mulDivDown(units, sellerPrice, WAD)` — denominator is always `WAD`, so it returns 0 without reverting. The NatSpec is incomplete and misleading for the equality case. ✓

---

Audit Report

## Title
`sellerAssetsToUnits` reverts on zero `sellerPrice` while `Midnight.take()` succeeds, blocking bundler fills at the settlement-fee price boundary - (File: src/periphery/TakeAmountsLib.sol)

## Summary
When `offer.buy = true` and `tickToPrice(offer.tick) == settlementFee(id, ttm)`, `sellerPrice` computes to zero in `sellerAssetsToUnits`. The subsequent call `targetSellerAssets.mulDivUp(WAD, 0)` triggers an underflow in `UtilsLib.mulDivUp`'s `d - 1` sub-expression, reverting unconditionally. Because `supplyCollateralAndSellWithAssetsTarget` invokes `sellerAssetsToUnits` outside its `try/catch` block, the entire transaction reverts, while a direct `Midnight.take()` with the same inputs succeeds and returns `sellerAssets = 0`.

## Finding Description

**Root cause — `mulDivUp` with denominator zero:**

`UtilsLib.mulDivUp` computes `(x * y + (d - 1)) / d`. When `d = 0`, `d - 1` underflows under Solidity 0.8 checked arithmetic, reverting unconditionally.

**Code path in `sellerAssetsToUnits`:**

```solidity
// TakeAmountsLib.sol lines 44-46
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...;
```
When `offer.buy = true` and `offerPrice == settlementFee`, `sellerPrice = 0`, and `mulDivUp(..., 0)` is called → unconditional revert.

**Contrast with `Midnight.take()`:**

`Midnight.take()` computes `sellerAssets = units.mulDivDown(sellerPrice, WAD)`. The denominator is always `WAD` (never zero), so `sellerAssets = units * 0 / WAD = 0` — no revert.

**Call site in `supplyCollateralAndSellWithAssetsTarget` — outside `try/catch`:**

```solidity
// MidnightBundles.sol lines 285-300
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(   // line 286 — outside try/catch
        MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
    ),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
);
try IMidnight(MIDNIGHT).take(...) returns (...) {  // line 292 — inside try/catch
    ...
} catch {}
```
The revert from `sellerAssetsToUnits` propagates unconditionally to the caller.

**Incorrect NatSpec comment:**

The comment at line 34 of `TakeAmountsLib.sol` states "Reverts if offerPrice < settlementFee in case of a buy offer (midnight reverts too)." At the boundary `offerPrice == settlementFee`, `Midnight.take()` does **not** revert — only the periphery helper does.

## Impact Explanation

Any caller of `supplyCollateralAndSellWithAssetsTarget` targeting a buy offer where `tickToPrice(offer.tick) == settlementFee(id, ttm)` receives an unconditional revert. The fill is completely blocked through the periphery path. No funds are lost (the transaction rolls back), but the bundler is rendered non-functional for this specific price boundary, creating a persistent DoS of the `supplyCollateralAndSellWithAssetsTarget` path for all affected offers.

## Likelihood Explanation

The condition requires exact equality between `tickToPrice(offer.tick)` and `settlementFee(id, ttm)`. Settlement fees are multiples of `CBP = 1e12`; tick prices are derived from `1e36 / (1e18 + wExp(...))`. Exact equality is possible but not guaranteed for arbitrary ticks. The most accessible trigger is at `ttm = 0` where the fee is the constant `settlementFeeCbp0 * CBP`. If the fee setter configures this value to match a specific tick price — even inadvertently — the condition is met for the entire post-maturity window and is repeatable for every call against that offer. Likelihood is low but non-zero and persistent once the alignment exists.

## Recommendation

Guard against zero `sellerPrice` in `sellerAssetsToUnits` before calling `mulDivUp`. When `sellerPrice == 0`, the inverse mapping is undefined (any number of units yields 0 seller assets), so the function should either revert with a descriptive error or return `type(uint256).max` to signal that no finite unit count can reach a nonzero `targetSellerAssets`. Additionally, update the NatSpec at line 34 to document that the revert also occurs at `offerPrice == settlementFee`, and that this diverges from `Midnight.take()` behavior at that boundary.

## Proof of Concept

1. Deploy or fork with a market where `settlementFeeCbp0 * CBP` equals `tickToPrice(t)` for some tick `t`.
2. Create a buy offer at tick `t`.
3. Call `supplyCollateralAndSellWithAssetsTarget` targeting that offer with any nonzero `targetSellerAssets`.
4. Observe unconditional revert from `sellerAssetsToUnits` → `mulDivUp(WAD, 0)`.
5. Call `Midnight.take()` directly with the same offer and any `units > 0`; observe success with `sellerAssets = 0`.

### Citations

**File:** src/libraries/UtilsLib.sol (L34-36)
```text
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
```

**File:** src/periphery/TakeAmountsLib.sol (L34-34)
```text
    /// @dev Reverts if offerPrice < settlementFee in case of a buy offer (midnight reverts too).
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
