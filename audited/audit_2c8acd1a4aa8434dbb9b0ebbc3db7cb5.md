Audit Report

## Title
`sellerAssetsToUnits` division-by-zero when `sellerPrice == 0` blocks bundler sell flows for affected buy offers - (`File: src/periphery/TakeAmountsLib.sol`)

## Summary
`TakeAmountsLib.sellerAssetsToUnits` computes `sellerPrice = offerPrice - settlementFee` for buy offers and passes it as the denominator to `mulDivUp(targetSellerAssets, WAD, sellerPrice)` with no guard against `sellerPrice == 0`. When the feeSetter sets the index-6 settlement fee to `MAX_SETTLEMENT_FEE_360_DAYS = 0.005e18` and a buy offer exists at the tick whose `tickToPrice` output equals exactly `0.005e18`, every call to `sellerAssetsToUnits` for that offer panics. Because this call in `MidnightBundles.supplyCollateralAndSellWithAssetsTarget` occurs before the `try/catch` wrapping `IMidnight.take`, the panic propagates and reverts the entire bundler transaction.

## Finding Description

**Root cause — missing zero guard in `sellerAssetsToUnits`:**

`src/periphery/TakeAmountsLib.sol` lines 41–46:
```solidity
uint256 offerPrice = TickLib.tickToPrice(offer.tick);
uint256 settlementFee =
    IMidnight(midnight).settlementFee(id, UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp));
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

There is no `require(sellerPrice > 0)` before the division. The sibling function `buyerAssetsToUnits` (lines 26–28) computes `sellerPrice` and then checks `require(buyerPrice <= WAD)`, but this only catches `buyerPrice > WAD`; it does not protect against `sellerPrice == 0`. `sellerAssetsToUnits` has no analogous guard at all.

**Why `sellerPrice == 0` is reachable:**

- `PRICE_ROUNDING_STEP = 1e12` (`TickLib.sol` line 8) and `MAX_SETTLEMENT_FEE_360_DAYS = 0.005e18 = 5e15` (`ConstantsLib.sol` line 17). Since `5e15` is a multiple of `1e12`, `tickToPrice` (which rounds to multiples of `PRICE_ROUNDING_STEP`) can produce exactly `0.005e18` for some tick `t*`.
- `maxSettlementFee(6) = MAX_SETTLEMENT_FEE_360_DAYS = 0.005e18`. Setting index 6 to `0.005e18` satisfies `newSettlementFee <= maxSettlementFee(index)` and `newSettlementFee % CBP == 0`, so it is a valid feeSetter action.
- For a market with TTM ≥ 360 days, `settlementFee(id, ttm) = settlementFeeCbp6 * CBP` (`Midnight.sol` line 967), which equals `0.005e18` under the above configuration. Thus `sellerPrice = 0.005e18 − 0.005e18 = 0`.

**Why `Midnight.take()` does NOT revert when `sellerPrice == 0`:**

`Midnight.sol` line 361: `uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;` — when `offerPrice == _settlementFee`, this is exactly `0` with no underflow. Line 364: `sellerAssets = units.mulDivDown(0, WAD) = 0`. No revert. The protocol comment at line 331 ("Taking buy offers with price < settlement fee will revert") covers only the strict underflow case (`offerPrice < settlementFee`), not the equality case.

**Why the bundler `try/catch` does NOT protect against this:**

`MidnightBundles.sol` lines 285–300:
```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(   // <-- panics here (line 286)
        MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
    ),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
);
try IMidnight(MIDNIGHT).take(...) returns (...) {   // <-- never reached
    ...
} catch {}
```

The arithmetic panic from `mulDivUp(..., 0)` is thrown at line 286 and propagates directly to the caller, reverting the entire bundler call. The `try/catch` at line 292 only wraps `IMidnight.take`, not the preceding `sellerAssetsToUnits` call.

## Impact Explanation
Every call to `supplyCollateralAndSellWithAssetsTarget` that includes the affected buy offer reverts unconditionally. Users cannot use the bundler to sell against that offer. The bundler sell path — the primary interface for asset-targeted selling — is frozen for all affected offers until the fee is lowered or the offer expires. Direct `Midnight.take()` still works (producing `sellerAssets = 0`), but the bundler path is completely blocked. This constitutes a concrete, permanent denial-of-service on a core user-facing function for all affected offers.

## Likelihood Explanation
The feeSetter setting the 360-day fee to its maximum (`0.005e18`) is a routine, explicitly permitted governance action — the protocol's own `maxSettlementFee` cap is designed to allow it. The tick whose price equals exactly `0.005e18` is a valid, accessible tick (price is a multiple of `PRICE_ROUNDING_STEP`). Any maker who placed a buy offer at that tick is affected. The condition is deterministic and repeatable: every bundler call with that offer will panic until the fee is lowered or the offer expires. No attacker action is required; the bug manifests from the combination of a legitimate governance action and a valid offer.

## Recommendation
Add a zero-guard in `sellerAssetsToUnits` before the division, mirroring the intent of the NatSpec comment ("Reverts if offerPrice < settlementFee in case of a buy offer"):

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
require(sellerPrice > 0, SellerPriceIsZero()); // or reuse an existing error
return offer.buy
    ? targetSellerAssets.mulDivUp(WAD, sellerPrice)
    : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

When `sellerPrice == 0`, the forward computation always yields `sellerAssets = 0` regardless of `units`, so the inverse is undefined for any `targetSellerAssets > 0`. Reverting with a meaningful error is the correct behavior and is consistent with how `Midnight.take()` is documented to handle the `offerPrice < settlementFee` case.

## Proof of Concept
1. Deploy Midnight and MidnightBundles on a local fork.
2. As feeSetter, call `setMarketSettlementFee(id, 6, 0.005e18)` for a market with TTM ≥ 360 days.
3. Find tick `t*` such that `TickLib.tickToPrice(t*) == 0.005e18` (iterate over ticks or binary-search).
4. Create a buy offer at tick `t*` (lender places offer with `offer.buy = true`, `offer.tick = t*`).
5. As any user, call `supplyCollateralAndSellWithAssetsTarget` with `takes[0].offer` set to the above buy offer and any nonzero `targetSellerAssets`.
6. Observe: the call reverts with an arithmetic panic (Solidity 0x12) thrown inside `sellerAssetsToUnits` at the `mulDivUp(targetSellerAssets, WAD, 0)` call, before the `try/catch` block is reached.
7. Confirm: calling `IMidnight.take` directly with the same offer and any `units` succeeds, returning `sellerAssets = 0`.