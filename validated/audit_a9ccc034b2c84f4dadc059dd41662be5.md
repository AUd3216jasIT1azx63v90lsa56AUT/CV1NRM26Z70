All code references check out. Let me verify the `CBP` constant and whether `settlementFee` output can exactly equal a tick price.

Audit Report

## Title
Division-by-Zero in `sellerAssetsToUnits` When `offerPrice == settlementFee` Causes DoS of `supplyCollateralAndSellWithAssetsTarget` - (File: src/periphery/TakeAmountsLib.sol)

## Summary

In `TakeAmountsLib.sellerAssetsToUnits`, when a buy offer's `offerPrice` equals the current `settlementFee`, `sellerPrice` is computed as zero and passed as the denominator `d` to `mulDivUp`. Because `mulDivUp` is implemented as `(x * y + (d - 1)) / d` outside any `unchecked` block, the sub-expression `(d - 1)` underflows under Solidity 0.8 checked arithmetic and reverts. Since `supplyCollateralAndSellWithAssetsTarget` calls `sellerAssetsToUnits` outside its `try/catch` block, any victim whose `takes[]` array includes such an offer has their entire bundler transaction reverted.

## Finding Description

**Root cause — `sellerAssetsToUnits` (`src/periphery/TakeAmountsLib.sol` lines 44–46):**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...
```

When `offer.buy == true` and `offerPrice == settlementFee`, `sellerPrice = 0`. `mulDivUp` (`src/libraries/UtilsLib.sol` lines 34–36) is:

```solidity
function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y + (d - 1)) / d;
}
```

This function is not in an `unchecked` block. With `d = 0`, `(d - 1)` underflows under Solidity 0.8 checked arithmetic and reverts with an arithmetic error — confirmed by the existing test `testMulDivUpDivisionByZero` (line 80–84 of `test/UtilsLibTest.sol`) which expects `stdError.arithmeticError`, not `stdError.divisionError`.

**Why `Midnight.take()` does NOT revert in the same state:** In `Midnight.sol` line 364, `sellerAssets = units.mulDivDown(sellerPrice, WAD)` — the denominator is `WAD` (1e18), not `sellerPrice`. With `sellerPrice = 0`, this yields `sellerAssets = 0` without reverting. The NatDoc comment "Reverts if offerPrice < settlementFee in case of a buy offer (midnight reverts too)" is only correct for the strict inequality; the equality case is a gap.

**Exploit path — `supplyCollateralAndSellWithAssetsTarget` (`src/periphery/MidnightBundles.sol` lines 285–291):**

```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(   // ← OUTSIDE try/catch; reverts here
        MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
    ),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)  // ← also calls sellerAssetsToUnits
);
try IMidnight(MIDNIGHT).take(...) { ... } catch {}  // never reached
```

Both `sellerAssetsToUnits` (line 286) and `ConsumableUnitsLib.consumableUnits` (line 290, which itself calls `sellerAssetsToUnits` at `src/periphery/ConsumableUnitsLib.sol` line 21 when `offer.buy && offer.maxAssets > 0`) are outside the `try/catch`. Either path causes the revert to propagate unconditionally to the caller.

**Attacker steps:**
1. Read `settlementFee(id, ttm)` — a public view function.
2. Find tick `T` where `tickToPrice(T) == settlementFee`. Both values are multiples of `PRICE_ROUNDING_STEP = 1e12` (proven by `tickToPriceUsesPriceRoundingStep` in `certora/specs/TickToPrice.spec`; settlement fees are multiples of `CBP = 1e12` at breakpoints).
3. Post a buy offer at tick `T` with any valid parameters. No privileged access required.
4. Any victim routing through this offer via `supplyCollateralAndSellWithAssetsTarget` has their entire transaction reverted.

**Existing protections reviewed and found insufficient:**
- The NatDoc guard covers only `offerPrice < settlementFee`, not `offerPrice == settlementFee`.
- `buyerAssetsToUnits` is unaffected because it divides by `buyerPrice = sellerPrice + settlementFee = settlementFee > 0`.
- There is no `require(sellerPrice > 0)` guard in `sellerAssetsToUnits`, unlike `buyerAssetsToUnits` which has `require(buyerPrice <= WAD)`.
- The `try/catch` in `supplyCollateralAndSellWithAssetsTarget` covers only `IMidnight.take()`, not the preceding `sellerAssetsToUnits` or `consumableUnits` calls.

## Impact Explanation

Any call to `supplyCollateralAndSellWithAssetsTarget` that includes a buy offer at the settlement-fee price point in its `takes[]` array reverts entirely. This permanently blocks the sell-via-periphery path for affected takers for as long as the offer exists at that tick. This constitutes service unavailability and severe degradation of a core periphery function.

## Likelihood Explanation

Settlement fees are public and readable on-chain. Both tick prices and settlement fees are multiples of `1e12`, so exact equality is structurally guaranteed to be reachable for any non-zero settlement fee within the tick range (5820 ticks spanning 0 to 1e18 in steps of 1e12). The attacker bears only gas cost with no capital at risk. The DoS persists until the offer is cancelled (by the attacker, who has no incentive to do so) or the settlement fee changes to a value with no matching tick. The attack is repeatable: if the fee changes, the attacker posts a new offer at the new matching tick.

## Recommendation

Add a guard in `sellerAssetsToUnits` mirroring the existing guard in `buyerAssetsToUnits`. Specifically, when `offer.buy == true`, require `sellerPrice > 0` before calling `mulDivUp`. Alternatively, return 0 when `sellerPrice == 0` (since no positive number of units can yield positive `sellerAssets` when `sellerPrice = 0`, the correct inverse is 0 units). A secondary hardening is to also wrap the `sellerAssetsToUnits` and `consumableUnits` calls in `supplyCollateralAndSellWithAssetsTarget` inside the `try/catch` block, consistent with the function's stated intent to skip all reasons why a take can revert.

## Proof of Concept

Minimal Foundry test:

1. Deploy a market and set a non-zero settlement fee at a breakpoint (e.g., `settlementFeeCbp1 = 1`, giving `settlementFee = 1e12` at exactly 1-day TTM).
2. Warp to exactly 1 day before maturity so `settlementFee(id, 1 days) = 1e12`.
3. Find tick `T` such that `TickLib.tickToPrice(T) == 1e12` (iterate over ticks 0–5820).
4. Have the attacker post a buy offer at tick `T` with `maxUnits > 0`.
5. Have the victim call `supplyCollateralAndSellWithAssetsTarget` with `takes[0]` pointing to that offer.
6. Assert the call reverts with `stdError.arithmeticError`.

The existing `testMulDivUpDivisionByZero` in `test/UtilsLibTest.sol` already confirms that `mulDivUp(x, y, 0)` reverts with `stdError.arithmeticError` for all `x, y`, providing the mechanical proof of the revert path.