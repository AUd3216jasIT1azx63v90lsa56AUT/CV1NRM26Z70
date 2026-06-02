All key code references have been verified against the actual source. Here is the validation result:

---

Audit Report

## Title
`TakeAmountsLib.sellerAssetsToUnits` panics on `sellerPrice == 0` outside `try/catch`, reverting entire bundle for a case `Midnight.take` accepts - (`src/periphery/TakeAmountsLib.sol` / `src/periphery/MidnightBundles.sol`)

## Summary
When a buy offer's `tickToPrice(offer.tick)` equals the current `settlementFee`, `sellerAssetsToUnits` computes `sellerPrice = 0` and calls `mulDivUp(targetSellerAssets, WAD, 0)`. The implementation `(x * y + (d - 1)) / d` underflows at `(0 - 1)` under Solidity 0.8 checked arithmetic before any division, producing an arithmetic panic. Because this call sits outside the `try/catch` in `supplyCollateralAndSellWithAssetsTarget`, the panic propagates and reverts the entire bundle — including all prior collateral supplies — even though `Midnight.take` would accept the same offer without reverting.

## Finding Description

**Root cause — `mulDivUp` with `d = 0`:**

`UtilsLib.sol` line 35:
```solidity
return (x * y + (d - 1)) / d;
```
When `d = 0`, the sub-expression `(d - 1)` = `(0 - 1)` triggers a Solidity 0.8 arithmetic underflow panic (code `0x11`) before any division.

**Trigger path in `sellerAssetsToUnits` (TakeAmountsLib.sol lines 41–46):**
```solidity
uint256 offerPrice   = TickLib.tickToPrice(offer.tick);
uint256 settlementFee = IMidnight(midnight).settlementFee(...);
uint256 sellerPrice  = offer.buy ? offerPrice - settlementFee : offerPrice;
return offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...;
```
When `offer.buy == true` and `offerPrice == settlementFee`, `sellerPrice = 0`, and `mulDivUp(..., 0)` panics.

**Location of the unguarded call (MidnightBundles.sol lines 285–300):**
```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(          // ← OUTSIDE try/catch
        MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
    ),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
);
try IMidnight(MIDNIGHT).take(...) { ... } catch {}  // ← only take() is guarded
```

**Why `Midnight.take` does not revert for the same input (Midnight.sol lines 361–364):**
```solidity
uint256 sellerPrice  = offer.buy ? offerPrice - _settlementFee : offerPrice; // = 0
uint256 buyerPrice   = sellerPrice + _settlementFee;                          // = settlementFee
uint256 buyerAssets  = units.mulDivDown(buyerPrice, WAD);  // divides by WAD
uint256 sellerAssets = units.mulDivDown(sellerPrice, WAD); // = 0, divides by WAD
```
`mulDivDown` is `(x * y) / d` with `d = WAD` — no panic. The take proceeds and returns `sellerAssets = 0`.

**Why `consumableUnits` does not panic for this offer:**
For `offer.buy == true` and `offer.maxAssets > 0` (with `offer.maxUnits == 0`), `ConsumableUnitsLib.sol` line 19 routes to `buyerAssetsToUnits`, which divides by `buyerPrice = offerPrice` (non-zero). No panic.

**Why existing checks fail:**
The NatSpec at MidnightBundles.sol line 246 explicitly states "Reverts if TakeAmountsLib or ConsumableUnitsLib reverts," acknowledging the gap. The `try/catch` at lines 292–300 only guards `Midnight.take`. The pre-computation calls are unconditionally executed. The NatSpec at TakeAmountsLib.sol line 15 says "Reverts if `offerPrice < settlementFee`" but the panic also fires at `offerPrice == settlementFee` (strict equality), which `Midnight.take` accepts.

## Impact Explanation
The entire `supplyCollateralAndSellWithAssetsTarget` transaction reverts. All collateral supplies executed in the loop at lines 269–275 (before the take loop) are rolled back. The taker cannot sell units or supply collateral even though all other offers in the bundle are valid and `Midnight.take` would accept them individually. This constitutes a denial-of-service on bundle execution: the taker is blocked and must identify and remove the offending offer before retrying. No funds are permanently lost (revert rolls back state), but the taker's collateral workflow is disrupted and gas is wasted.

## Likelihood Explanation
**Required preconditions:**
1. A buy offer exists with `offer.buy == true`, `offer.maxAssets > 0`, `offer.maxUnits == 0`.
2. `tickToPrice(offer.tick) == settlementFee(id, timeToMaturity)` holds at call time.
3. `targetSellerAssets > 0` (loop executes).

**Feasibility:** Settlement fees are interpolated as a function of time-to-maturity. Tick prices are a discrete set of values. For any market with a non-zero settlement fee schedule, there exist moments in time when the interpolated fee crosses a tick-price boundary and equals `tickToPrice(t)` for some tick `t`. A malicious offer maker can create a buy offer at that tick. An automated taker that fetches all available offers and includes them in a bundle will include this offer. When the settlement fee equals the tick price at the moment of execution, the bundle panics. The condition is transient (holds only at specific time windows) but is repeatable and can be targeted by computing the fee schedule and placing offers at matching ticks in advance. The comment in Midnight.sol ("if the settlement fee gets increased, it might implicitly cancel offers with very low price") confirms the protocol designers are aware that fee/price equality is reachable.

## Recommendation
Move the `sellerAssetsToUnits` call inside the `try/catch`, or add an explicit guard before calling it:

**Option A — guard before the call:**
```solidity
uint256 offerPrice = TickLib.tickToPrice(takes[i].offer.tick);
uint256 fee = IMidnight(MIDNIGHT).settlementFee(id, ...);
if (takes[i].offer.buy && offerPrice <= fee) continue; // skip: sellerPrice == 0
```

**Option B — wrap pre-computation in try/catch:**
Restructure so that `sellerAssetsToUnits` and `consumableUnits` are called inside a `try/catch` block, treating any revert from them as a signal to skip the offer (consistent with the bundler's stated design intent of skipping offers that cannot be taken).

**Option C — fix `sellerAssetsToUnits` NatSpec and add a zero-check:**
Add `require(sellerPrice > 0)` inside `sellerAssetsToUnits` with a descriptive error, and update the NatSpec to say "Reverts if `offerPrice <= settlementFee`." This makes the revert a typed error rather than a panic, but does not fix the bundler's failure to guard the call.

The most robust fix is Option B, which aligns with the bundler's documented intent to skip any offer that cannot be taken.

## Proof of Concept
**Minimal fork test plan:**
1. Deploy Midnight with a market whose settlement fee schedule passes through `tickToPrice(T)` for some tick `T` at a known future timestamp.
2. Create a buy offer at tick `T` with `offer.buy = true`, `offer.maxAssets > 0`, `offer.maxUnits = 0`.
3. Warp block timestamp to the moment when `settlementFee(id, maturity - block.timestamp) == tickToPrice(T)`.
4. Call `supplyCollateralAndSellWithAssetsTarget` with `takes = [{offer: <offer at tick T>, units: 1, ...}]` and `targetSellerAssets = 1`.
5. Observe: transaction reverts with arithmetic panic `0x11` at `UtilsLib.mulDivUp`.
6. Confirm: calling `IMidnight.take(<offer at tick T>, ..., 1, ...)` directly at the same timestamp succeeds and returns `sellerAssets = 0`.