### Title
Division by Zero in `TakeAmountsLib.sellerAssetsToUnits` Causes DoS in `MidnightBundles.supplyCollateralAndSellWithAssetsTarget` — (File: src/periphery/TakeAmountsLib.sol)

---

### Summary

`TakeAmountsLib.sellerAssetsToUnits` computes `units` from a target seller asset amount by dividing by `sellerPrice`. For buy offers, `sellerPrice = offerPrice - settlementFee`. When `offerPrice == settlementFee`, `sellerPrice` is zero, causing an unguarded division by zero. This function is called unconditionally inside `MidnightBundles.supplyCollateralAndSellWithAssetsTarget`, causing the entire bundler transaction to revert for any buy offer at this price boundary.

---

### Finding Description

In `TakeAmountsLib.sellerAssetsToUnits`: [1](#0-0) 

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

For a buy offer, `sellerPrice = offerPrice - settlementFee`. There is no guard checking `sellerPrice > 0` before the division. If `offerPrice == settlementFee`, `sellerPrice == 0` and `mulDivUp(WAD, 0)` performs integer division by zero, reverting. [2](#0-1) 

This function is called directly (not inside a `try/catch`) in `MidnightBundles.supplyCollateralAndSellWithAssetsTarget`: [3](#0-2) 

The bundler's own NatSpec at line 175 acknowledges: *"Reverts if TakeAmountsLib or ConsumableUnitsLib reverts."* — confirming this revert propagates to the caller. [4](#0-3) 

**Trigger condition**: A buy offer exists where `tickToPrice(offer.tick) == settlementFee(id, timeToMaturity)`. Both values are in the same WAD-scaled range. For example, at near-zero settlement fees (e.g., `settlementFeeCbp ≈ 1`), the lowest-tick prices (~1e12) can equal the fee. This is a realistic, constructible state.

---

### Impact Explanation

Any call to `supplyCollateralAndSellWithAssetsTarget` that includes a buy offer satisfying `offerPrice == settlementFee` reverts entirely. Because collateral supply happens before the take loop, the revert also rolls back the collateral supply. A borrower attempting to atomically supply collateral and borrow via the bundler is completely blocked for that offer set. They must fall back to calling `Midnight.take` directly, losing the atomicity guarantee of the bundler.

---

### Likelihood Explanation

No privileged access is required. Any market participant can post a buy offer at a tick whose price equals the current settlement fee. Settlement fees are public on-chain state. A malicious maker can deliberately construct such an offer to grief users of `supplyCollateralAndSellWithAssetsTarget`. The condition is also reachable non-maliciously when the settlement fee setter adjusts fees to a value that coincides with an existing offer's price.

---

### Recommendation

Add a zero-check for `sellerPrice` in `sellerAssetsToUnits`, mirroring the pattern used in `buyerAssetsToUnits` which already guards against `buyerPrice > WAD`:

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
if (sellerPrice == 0) return type(uint256).max; // or revert with a descriptive error
return offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

Returning `type(uint256).max` is consistent with the bundler's `min(...)` pattern — it would cause `unitsToTake` to be capped by `takes[i].units` or `consumableUnits`, effectively skipping the offer gracefully. [1](#0-0) 

---

### Proof of Concept

1. Deploy `Midnight` and `MidnightBundles`.
2. Set `settlementFeeCbp` for a market such that `settlementFee(id, ttm) == tickToPrice(T)` for some tick `T`.
3. Maker posts a buy offer at tick `T` with `maxAssets > 0`.
4. Borrower calls `supplyCollateralAndSellWithAssetsTarget` with this offer in the `takes` array.
5. Execution reaches `TakeAmountsLib.sellerAssetsToUnits(MIDNIGHT, id, offer, ...)`.
6. `sellerPrice = tickToPrice(T) - settlementFee = 0`.
7. `mulDivUp(WAD, 0)` → division by zero → entire transaction reverts.
8. Borrower's collateral supply is also rolled back; the operation cannot complete atomically. [5](#0-4) [6](#0-5)

### Citations

**File:** src/periphery/TakeAmountsLib.sol (L36-47)
```text
    function sellerAssetsToUnits(address midnight, bytes32 id, Offer memory offer, uint256 targetSellerAssets)
        internal
        view
        returns (uint256)
    {
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
        uint256 settlementFee =
            IMidnight(midnight).settlementFee(id, UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp));
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        return
            offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
    }
```

**File:** src/periphery/MidnightBundles.sol (L175-176)
```text
    /// @dev Reverts if TakeAmountsLib or ConsumableUnitsLib reverts.
    /// @dev If taking an offer reverts, the bundler will completely skip this offer.
```

**File:** src/periphery/MidnightBundles.sol (L282-301)
```text
        for (uint256 i; i < takes.length && filledSellerAssets < targetFilledSellerAssets; i++) {
            require(takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
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
        }
```
