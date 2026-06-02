All code references check out. Here is the validation result:

---

Audit Report

## Title
`buyWithAssetsTargetAndWithdrawCollateral` reverts for sell offers where `offerPrice + settlementFee > WAD` due to uncaught revert in `buyerAssetsToUnits` - (File: src/periphery/TakeAmountsLib.sol)

## Summary
In `MidnightBundles.buyWithAssetsTargetAndWithdrawCollateral`, the call to `TakeAmountsLib.buyerAssetsToUnits` is placed outside the `try/catch` block that wraps `IMidnight.take`. For sell offers where `offerPrice + settlementFee > WAD`, `buyerAssetsToUnits` reverts with `PriceGreaterThanOne()`, causing the entire transaction to revert. Midnight's core `take()` function has no equivalent guard and would accept the same offer, creating a strict divergence between periphery and core behavior.

## Finding Description

**Root cause:** `TakeAmountsLib.sol` line 28:
```solidity
require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne());
```
For sell offers (`offer.buy = false`), lines 26–27 compute:
```solidity
uint256 sellerPrice = offerPrice;          // false branch
uint256 buyerPrice  = sellerPrice + settlementFee;  // = offerPrice + settlementFee
```
This `require` is absent in `Midnight.take()` (lines 361–363), which computes the same `buyerPrice` and proceeds unconditionally to `units.mulDivUp(buyerPrice, WAD)`.

**Structural gap:** In `MidnightBundles.buyWithAssetsTargetAndWithdrawCollateral` (lines 208–221), `buyerAssetsToUnits` is evaluated as an argument to `min(...)` *before* the `try` keyword:
```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.buyerAssetsToUnits(...),   // NOT in try/catch — reverts propagate
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(...)
);
try IMidnight(MIDNIGHT).take(...) { ... } catch {}  // only take() is guarded
```
The NatSpec at line 175 documents this: *"Reverts if TakeAmountsLib or ConsumableUnitsLib reverts."* This is a design gap, not an intentional invariant, because `take()` itself would succeed on the same offer.

**Reachability:** From `TickLib.sol`:
- `MAX_TICK = 5820`; `tickToPrice(5820) ≈ WAD − 5e11` (≈ `1e18 − 500`)

From `ConstantsLib.sol`:
- `MAX_SETTLEMENT_FEE_360_DAYS = 0.005e18 = 5e15`

Therefore `offerPrice + settlementFee ≈ (WAD − 5e11) + 5e15 ≈ WAD + 5e15 > WAD`. The condition is triggered for any sell offer with `tick` near `MAX_TICK` in any market with a non-zero settlement fee.

**Exploit flow:**
1. Maker creates a sell offer (`offer.buy = false`) at `tick = MAX_TICK = 5820` in a market with any non-zero settlement fee (e.g., `settlementFeeCbp6 = 1`, giving `settlementFee = 1e12`).
2. Taker calls `buyWithAssetsTargetAndWithdrawCollateral` with this offer in `takes[]`.
3. Loop iteration reaches this offer; `buyerAssetsToUnits` is called with `buyerPrice ≈ WAD + 5e15`.
4. `require(buyerPrice <= WAD)` reverts with `PriceGreaterThanOne()`.
5. Revert propagates out of `min(...)`, bypassing the `try/catch`, and the entire transaction reverts.
6. Calling `IMidnight.take()` directly on the same offer succeeds — no equivalent check exists in core.

## Impact Explanation
`buyWithAssetsTargetAndWithdrawCollateral` is permanently unusable for any sell offer where `tickToPrice(offer.tick) + settlementFee(id, ttm) > WAD`. For markets with the maximum 360-day settlement fee of `0.005e18`, this affects all sell offers at or near `tick = MAX_TICK`. Users are forced to call `IMidnight.take()` directly or use `buyWithUnitsTargetAndWithdrawCollateral`, defeating the purpose of the assets-target bundle. Any taker transaction that includes even one such offer in its `takes[]` array will revert entirely, including all other valid offers in the same call.

## Likelihood Explanation
The condition is deterministically reachable without any special privileges, oracle manipulation, or admin action. A maker simply places a sell offer at a high tick — a normal protocol action — in any market with a non-zero settlement fee. `tickToPrice(MAX_TICK) ≈ WAD − 5e11` and `MAX_SETTLEMENT_FEE_360_DAYS = 5e15 >> 5e11`, so the margin is large. The condition holds for any settlement fee greater than ~500 (i.e., `settlementFeeCbp6 ≥ 1`). No victim mistake is required; the taker's transaction simply reverts.

## Recommendation
Wrap the entire per-offer computation — including `buyerAssetsToUnits` and `ConsumableUnitsLib.consumableUnits` — in a `try/catch` block, or add an explicit pre-check that skips offers where `tickToPrice(offer.tick) + settlementFee(id, ttm) > WAD` (using a `continue`). This is consistent with the existing intent of the `try/catch` around `take()`: to skip offers that would revert for any reason, including asynchrony-unrelated ones. The NatSpec comment at line 175 should be updated to reflect the corrected behavior.

## Proof of Concept
**Minimal manual steps:**
1. Deploy `Midnight` and `MidnightBundles`.
2. Create a market with `settlementFeeCbp6 = 1` (1 cbp = `1e12` WAD).
3. Maker creates a sell offer with `tick = 5820` (= `MAX_TICK`), `offer.buy = false`.
4. Taker calls `buyWithAssetsTargetAndWithdrawCollateral` with `takes = [{offer, ...}]`.
5. Observe: transaction reverts with `PriceGreaterThanOne()`.
6. Taker calls `IMidnight(midnight).take(offer, ..., units, taker, ...)` directly.
7. Observe: transaction succeeds, confirming core accepts the offer that the periphery rejects.

**Fuzz test plan:** Fuzz `tick ∈ [0, MAX_TICK]` and `settlementFeeCbp6 ∈ [1, 500]`; assert that whenever `tickToPrice(tick) + settlementFeeCbp6 * CBP > WAD`, `buyWithAssetsTargetAndWithdrawCollateral` reverts while `IMidnight.take` succeeds on the same offer. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** src/periphery/TakeAmountsLib.sol (L26-28)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + settlementFee;
        require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne());
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

**File:** src/Midnight.sol (L358-363)
```text
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
        uint256 _settlementFee = settlementFee(id, timeToMaturity);
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
```

**File:** src/libraries/TickLib.sol (L6-6)
```text
uint256 constant MAX_TICK = 5820;
```

**File:** src/libraries/ConstantsLib.sol (L17-17)
```text
uint256 constant MAX_SETTLEMENT_FEE_360_DAYS = 0.005e18;
```
