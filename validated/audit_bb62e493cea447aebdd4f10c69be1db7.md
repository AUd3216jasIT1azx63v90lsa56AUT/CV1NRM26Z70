Audit Report

## Title
`sellerAssetsToUnits` reverts on `sellerPrice = 0` while `Midnight.take()` succeeds, causing `supplyCollateralAndSellWithAssetsTarget` to revert for valid buy offers - (File: src/periphery/TakeAmountsLib.sol)

## Summary

When a buy offer is placed at tick `T` where `tickToPrice(T) == settlementFee(id, ttm)`, `sellerPrice` computes to exactly `0`. `TakeAmountsLib.sellerAssetsToUnits` then calls `mulDivUp(targetSellerAssets, WAD, 0)`, which triggers an arithmetic underflow at `d - 1` (Solidity 0.8.x checked arithmetic), reverting the call. `Midnight.take()` does not revert in this case — it computes `sellerAssets = units.mulDivDown(0, WAD) = 0` and proceeds normally. Because `sellerAssetsToUnits` is called outside the `try/catch` block in `MidnightBundles.supplyCollateralAndSellWithAssetsTarget`, the revert propagates out of the bundler entirely, permanently blocking the taker from using this function when such an offer appears in the `takes` array.

## Finding Description

**Root cause — `mulDivUp` with `d = 0`:**

`UtilsLib.mulDivUp` is implemented as:

```solidity
// src/libraries/UtilsLib.sol, line 34-36
function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y + (d - 1)) / d;
}
```

When `d = 0`, the expression `(d - 1)` = `uint256(0) - 1` underflows under Solidity 0.8.x checked arithmetic, reverting unconditionally. This is confirmed by the existing test at `test/UtilsLibTest.sol` lines 80–84, which explicitly expects `arithmeticError` when `d = 0`.

**Code path in `TakeAmountsLib.sellerAssetsToUnits`:**

```solidity
// src/periphery/TakeAmountsLib.sol, lines 41-46
uint256 offerPrice = TickLib.tickToPrice(offer.tick);
uint256 settlementFee = IMidnight(midnight).settlementFee(id, ...);
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return offer.buy
    ? targetSellerAssets.mulDivUp(WAD, sellerPrice)   // d = 0 → REVERT
    : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

When `offer.buy = true` and `offerPrice == settlementFee`, `sellerPrice = 0`. The call becomes `mulDivUp(targetSellerAssets, WAD, 0)`, which reverts via the `d - 1` underflow.

**Contrast with `Midnight.take()`:**

```solidity
// src/Midnight.sol, lines 361-364
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;  // = 0, no underflow
uint256 buyerPrice  = sellerPrice + _settlementFee;                           // = settlementFee
uint256 buyerAssets  = offer.buy ? units.mulDivDown(buyerPrice, WAD) : ...;  // fine
uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : ...; // = 0, fine
```

`mulDivDown(units, 0, WAD) = (units * 0) / WAD = 0` — no revert. `Midnight.take()` succeeds and yields `sellerAssets = 0`.

**Call site in `MidnightBundles.supplyCollateralAndSellWithAssetsTarget`:**

```solidity
// src/periphery/MidnightBundles.sol, lines 285-300
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(          // ← NOT inside try/catch; reverts here
        MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
    ),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
);
try IMidnight(MIDNIGHT).take(...) returns (...) { ... } catch {}  // ← try/catch only wraps take()
```

The `sellerAssetsToUnits` call is outside the `try/catch`. The bundler's own NatSpec at line 246 confirms: *"Reverts if TakeAmountsLib or ConsumableUnitsLib reverts."*

**Why the documented NatSpec does not cover this case:**

The NatSpec on `sellerAssetsToUnits` (line 34) states: *"Reverts if offerPrice < settlementFee in case of a buy offer (midnight reverts too)."* This is correct for the strict-less-than case (Solidity 0.8.x underflow on `offerPrice - settlementFee` at line 44). But the equal case (`offerPrice == settlementFee`) is not handled: `Midnight.take()` accepts `sellerPrice = 0` and returns `sellerAssets = 0`, while `sellerAssetsToUnits` reverts via `mulDivUp`'s `d - 1` underflow. The divergence is real and undocumented.

## Impact Explanation

Any call to `supplyCollateralAndSellWithAssetsTarget` that includes a buy offer at tick `T` where `tickToPrice(T) == settlementFee(id, ttm)` reverts entirely. The taker cannot complete the bundled sell-with-assets-target operation through this offer. If the attacker's offer is the only or first offer in the `takes` array, the entire bundler call fails, preventing the taker from borrowing against their collateral via the bundler. The attacker's offer is never consumed (the bundler reverts before `take()` is called), so the griefing is permanent and repeatable at zero cost beyond the initial offer placement.

## Likelihood Explanation

**Preconditions:**
1. A buy offer exists at tick `T` where `tickToPrice(T) == settlementFee(id, ttm)`. Both `tickToPrice` and `settlementFee` are multiples of `CBP = 1e12`. Since `tickToPrice` is monotonically increasing from tick 0 to `MAX_TICK = 5820` and the maximum settlement fee at 360 days is `0.005e18`, there exist low-tick values whose price (a multiple of 1e12) equals a valid settlement fee (also a multiple of 1e12). An attacker can enumerate ticks offline and post an offer at the matching tick — no privileged access required.
2. A taker routes through this offer via `supplyCollateralAndSellWithAssetsTarget` — possible if the offer appears in an off-chain order book and is included in the `takes` array.
3. `targetFilledSellerAssets - filledSellerAssets > 0` — always true inside the loop due to the `filledSellerAssets < targetFilledSellerAssets` loop guard.

The attack is repeatable: the attacker can maintain the offer indefinitely since the bundler reverts before `take()` is called, so the offer is never consumed.

## Recommendation

In `TakeAmountsLib.sellerAssetsToUnits`, add an explicit guard for the `sellerPrice == 0` case before calling `mulDivUp`. When `sellerPrice = 0`, `Midnight.take()` yields `sellerAssets = 0` for any number of units, so no finite unit count can satisfy a positive `targetSellerAssets`. The function should return `0` (or `type(uint256).max`) to signal that this offer cannot contribute to the target, causing `min(0, takes[i].units, consumableUnits) = 0` and allowing the bundler to skip the offer gracefully:

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
if (offer.buy && sellerPrice == 0) return 0; // or type(uint256).max
return offer.buy
    ? targetSellerAssets.mulDivUp(WAD, sellerPrice)
    : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

Alternatively, the NatSpec should be updated to document the equal case, and the bundler should wrap the `sellerAssetsToUnits` call in a `try/catch` consistent with how `take()` is already handled.

## Proof of Concept

1. Deploy a market with maturity `M` and a settlement fee schedule such that `settlementFee(id, M - block.timestamp) = F` where `F` is a multiple of `1e12`.
2. Find tick `T` such that `tickToPrice(T) == F` (enumerate offline; both are multiples of `1e12`).
3. Post a buy offer at tick `T` with `offer.buy = true`.
4. Call `supplyCollateralAndSellWithAssetsTarget` with `takes = [{offer: <the above offer>, units: N, ...}]` and any positive `targetSellerAssets`.
5. Observe: the call reverts at `TakeAmountsLib.sellerAssetsToUnits` with an arithmetic underflow, even though a direct call to `Midnight.take()` with the same offer and `units = N` succeeds and returns `sellerAssets = 0`.
6. Confirm the offer is never consumed (its `consumed` storage slot is unchanged), so the attack can be repeated indefinitely.