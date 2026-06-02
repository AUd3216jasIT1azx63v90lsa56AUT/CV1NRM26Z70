Audit Report

## Title
Division-by-Zero in `sellerAssetsToUnits` When `tickToPrice(tick) == settlementFee` Causes DoS of `supplyCollateralAndSellWithAssetsTarget` - (File: src/periphery/TakeAmountsLib.sol)

## Summary

`sellerAssetsToUnits` computes `sellerPrice = offerPrice - settlementFee` and then calls `mulDivUp(targetSellerAssets, WAD, sellerPrice)`. When `offerPrice == settlementFee`, `sellerPrice = 0`, and `mulDivUp` reverts via Solidity 0.8 checked arithmetic underflow at `(d - 1)` with `d = 0`. Critically, `Midnight.take()` does not revert in this state — it simply yields `sellerAssets = 0` — so the NatDoc invariant "midnight reverts too" is false for the equality case. Because `supplyCollateralAndSellWithAssetsTarget` calls `sellerAssetsToUnits` outside its `try/catch` block, any victim whose `takes[]` array includes such an offer has their entire bundler transaction reverted.

## Finding Description

**Root cause — `sellerAssetsToUnits` (`src/periphery/TakeAmountsLib.sol` lines 41–46):**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...
```

When `offer.buy == true` and `offerPrice == settlementFee`, `sellerPrice = 0`. `mulDivUp` is implemented as:

```solidity
// src/libraries/UtilsLib.sol line 35
return (x * y + (d - 1)) / d;
```

With `d = 0`, the sub-expression `(d - 1)` underflows under Solidity 0.8 checked arithmetic and reverts with an arithmetic error. This is confirmed by the existing test `testMulDivUpDivisionByZero` which expects `stdError.arithmeticError` (not `divisionError`) precisely because the revert occurs at `(d - 1)`, not at the division.

**Why `Midnight.take()` does NOT revert in the same state (`src/Midnight.sol` lines 361–364):**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;  // = 0
uint256 buyerPrice  = sellerPrice + _settlementFee;                           // = settlementFee
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : ...;   // divides by WAD, fine
uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : ...; // = 0 * units / WAD = 0, fine
```

`mulDivDown` is `(x * y) / d` — the denominator is `WAD` (1e18), not `sellerPrice`. No revert occurs.

**Exploit path — `supplyCollateralAndSellWithAssetsTarget` (`src/periphery/MidnightBundles.sol` lines 285–291):**

```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(   // ← OUTSIDE try/catch; reverts here
        MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
    ),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
);
try IMidnight(MIDNIGHT).take(...) { ... } catch {}  // never reached
```

The `sellerAssetsToUnits` call is not wrapped in the `try/catch`. Its revert propagates unconditionally to the caller.

**Attacker steps:**
1. Read the current `settlementFee(id, ttm)` — a public view function.
2. Call `TickLib.priceToTick(settlementFee, tickSpacing)` to find tick `T` where `tickToPrice(T) == settlementFee`. Both values are multiples of `PRICE_ROUNDING_STEP = 1e12`, so exact equality is structurally reachable.
3. Post a buy offer at tick `T` with any valid parameters. No privileged access required.
4. Any victim who routes through this offer via `supplyCollateralAndSellWithAssetsTarget` has their entire transaction reverted.

**Existing protections reviewed and found insufficient:**
- The NatDoc comment `"Reverts if offerPrice < settlementFee in case of a buy offer (midnight reverts too)"` is correct only for the strict inequality. The equality case (`offerPrice == settlementFee`) is not covered: `midnight.take()` succeeds but `sellerAssetsToUnits` reverts.
- `buyerAssetsToUnits` is unaffected because it divides by `buyerPrice = sellerPrice + settlementFee = settlementFee > 0`.
- There is no `require(sellerPrice > 0)` guard in `sellerAssetsToUnits`.
- The `try/catch` in `supplyCollateralAndSellWithAssetsTarget` covers only `IMidnight.take()`, not the preceding `sellerAssetsToUnits` call.

## Impact Explanation

Any call to `supplyCollateralAndSellWithAssetsTarget` that includes a buy offer at the settlement-fee price point in its `takes[]` array reverts entirely. This permanently blocks the sell-via-periphery path for affected takers for as long as the offer exists at that tick. The attacker bears only the gas cost of posting one buy offer; no capital is at risk. This constitutes service unavailability and severe degradation of a core periphery function for any user routing through the affected offer.

## Likelihood Explanation

Settlement fees are public and readable on-chain. Both tick prices and settlement fees are multiples of `1e12`, so exact equality is structurally guaranteed to be reachable for any non-zero settlement fee within the tick range. The attacker bears only gas cost with no capital at risk. The DoS persists until the offer is cancelled (by the attacker, who has no incentive to do so) or the settlement fee changes to a value with no matching tick. The attack is repeatable: if the fee changes, the attacker posts a new offer at the new matching tick.

## Recommendation

The most robust fix is to handle the `sellerPrice == 0` case explicitly in `sellerAssetsToUnits`. When `sellerPrice == 0`, the seller receives zero assets regardless of units taken, so no finite number of units can satisfy a positive `targetSellerAssets`. The function should either revert with a descriptive error or return `type(uint256).max` (which will be capped by `takes[i].units` in the caller, effectively skipping the offer). A secondary defense is to wrap the `sellerAssetsToUnits` call inside `supplyCollateralAndSellWithAssetsTarget` in a `try/catch` consistent with the existing pattern for `IMidnight.take()`, so that a reverting helper causes the offer to be skipped rather than the entire transaction to revert.

## Proof of Concept

1. Deploy a market with a non-zero settlement fee `F` that is a multiple of `1e12`.
2. Compute `T = TickLib.priceToTick(F, tickSpacing)` — the lowest tick whose price equals `F`.
3. As an unprivileged attacker, post a buy offer at tick `T` with any valid `maker`, `group`, `maxUnits`, etc.
4. As a victim, call `supplyCollateralAndSellWithAssetsTarget` with `takes[0]` set to the attacker's offer and any positive `targetSellerAssets`.
5. Observe the transaction reverts with an arithmetic underflow error originating from `UtilsLib.mulDivUp` inside `TakeAmountsLib.sellerAssetsToUnits`.
6. Confirm that calling `IMidnight.take()` directly on the same offer with any `units > 0` succeeds and returns `sellerAssets = 0`.