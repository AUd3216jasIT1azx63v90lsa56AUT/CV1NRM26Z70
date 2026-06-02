All claims are verified against the actual code. Here is the validation result:

---

Audit Report

## Title
`sellerAssetsToUnits` reverts with arithmetic underflow when `sellerPrice == 0` on a valid buy offer at the settlement fee boundary - (File: `src/periphery/TakeAmountsLib.sol`)

## Summary
`TakeAmountsLib.sellerAssetsToUnits` calls `targetSellerAssets.mulDivUp(WAD, sellerPrice)` where `sellerPrice = offerPrice - settlementFee` for buy offers. When `offerPrice == settlementFee`, `sellerPrice` is zero and `UtilsLib.mulDivUp` computes `(d - 1)` with `d = 0`, triggering an arithmetic underflow revert under Solidity 0.8 checked arithmetic. Because `supplyCollateralAndSellWithAssetsTarget` calls `sellerAssetsToUnits` outside its `try/catch` block, the revert propagates and DoSes the entire bundler transaction for any user whose `takes[]` array includes such an offer.

## Finding Description

**Root cause — `UtilsLib.mulDivUp` with `d = 0`:**

`UtilsLib.mulDivUp` is implemented as: [1](#0-0) 

With `d = 0`, the sub-expression `(d - 1)` underflows under Solidity 0.8 checked arithmetic before any division occurs.

**`TakeAmountsLib.sellerAssetsToUnits` — zero denominator path:**

For buy offers, `sellerPrice` is computed as `offerPrice - settlementFee`: [2](#0-1) 

When `offer.buy == true` and `tickToPrice(offer.tick) == settlementFee(id, ttm)`, line 44 yields `sellerPrice = 0`. Line 46 then calls `targetSellerAssets.mulDivUp(WAD, 0)` → arithmetic revert. The NatSpec at line 34 only documents the `offerPrice < settlementFee` revert (underflow in subtraction), not the `offerPrice == settlementFee` case (zero denominator in the inverse): [3](#0-2) 

**Why `Midnight.take()` accepts this offer:**

In the core protocol, `sellerPrice = 0` is used in the forward direction via `mulDivDown`. `units.mulDivDown(0, WAD) = (units * 0) / WAD = 0` — no revert. The seller receives zero assets and all buyer payment goes to the settlement fee. The core protocol does not reject `offerPrice == settlementFee` for buy offers.

**Why the bundler call reverts entirely:**

`supplyCollateralAndSellWithAssetsTarget` calls `sellerAssetsToUnits` at line 286 **outside** the `try/catch` block that wraps `IMidnight.take()`: [4](#0-3) 

The `try/catch` at line 292 only catches reverts from `take()` itself. The NatSpec at line 246 explicitly acknowledges this: [5](#0-4) 

The loop condition `filledSellerAssets < targetFilledSellerAssets` (line 282) guarantees `targetFilledSellerAssets - filledSellerAssets > 0` when `sellerAssetsToUnits` is called, so `targetSellerAssets > 0` always — the zero-numerator escape does not apply.

**Fuzz test coverage gap:**

All fuzz tests in `TakeAmountsTest.sol` set `offer.buy = false` in `setUp()` and never exercise the buy-offer path through `sellerAssetsToUnits`: [6](#0-5) 

The `_maxTick` helper bounds ticks to ensure `buyerPrice <= WAD` for sell offers, but does not exclude `sellerPrice == 0` for buy offers: [7](#0-6) 

## Impact Explanation
Any call to `supplyCollateralAndSellWithAssetsTarget` that includes a buy offer where `tickToPrice(offer.tick) == settlementFee(id, ttm)` at call time reverts entirely. The taker's authorized bundler flow is completely blocked for that transaction. Collateral already supplied within the same call (lines 269–275) is locked in the bundler until the transaction reverts and unwinds. The attack is a permissionless, repeatable griefing DoS against any user of the `supplyCollateralAndSellWithAssetsTarget` bundler function. [8](#0-7) 

## Likelihood Explanation
**Preconditions:**
1. A buy offer exists with `tickToPrice(offer.tick) == settlementFee(id, ttm)` at the time of the bundler call.
2. A victim taker has authorized the bundler and includes this offer in `takes[]`.

**Feasibility:** Settlement fees are set in multiples of `1e12` and tick prices are discrete values from `tickToPrice`. An attacker can enumerate tick prices and find one matching the current or near-future settlement fee. The condition is repeatable: the attacker can create a new offer each time the settlement fee drifts to a matching tick price. No privileged access is required — any address can create a buy offer.

## Recommendation
In `TakeAmountsLib.sellerAssetsToUnits`, add an explicit guard for the `sellerPrice == 0` case before calling `mulDivUp`. When `sellerPrice == 0`, the inverse is undefined (any number of units yields zero seller assets), so the function should either revert with a descriptive error (e.g., `require(sellerPrice > 0, ZeroSellerPrice())`) or return `type(uint256).max` to signal that no finite unit count can reach the target. The NatSpec should be updated to document this boundary. Separately, the fuzz tests in `TakeAmountsTest.sol` should be extended to cover `offer.buy = true` paths, including the `sellerPrice == 0` boundary. [9](#0-8) 

## Proof of Concept
1. Deploy the protocol and create a market with a known `settlementFee`.
2. Find a tick `t` such that `tickToPrice(t) == settlementFee(id, ttm)`.
3. Create a buy offer at tick `t` with sufficient `maxUnits`.
4. As a victim taker who has authorized the bundler, call `supplyCollateralAndSellWithAssetsTarget` with `takes[0]` set to the above buy offer and any nonzero `targetSellerAssets`.
5. Observe the transaction reverts with an arithmetic underflow panic (0x11) originating from `UtilsLib.mulDivUp` via `TakeAmountsLib.sellerAssetsToUnits`, before `IMidnight.take()` is ever called.

A minimal unit test can be written in `TakeAmountsTest.sol` by setting `offer.buy = true`, setting `offer.tick` to a tick whose price equals the current settlement fee, and calling `TakeAmountsLib.sellerAssetsToUnits(address(midnight), id, offer, 1)` — this will revert with `Panic(0x11)`.

### Citations

**File:** src/libraries/UtilsLib.sol (L34-36)
```text
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
```

**File:** src/periphery/TakeAmountsLib.sol (L32-35)
```text
    /// @dev Forward: sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD).
    /// @dev Assumes that id and offer.market match.
    /// @dev Reverts if offerPrice < settlementFee in case of a buy offer (midnight reverts too).
    /// @dev Returns a number of units for which take yields exactly targetSellerAssets (not necessarily the smallest).
```

**File:** src/periphery/TakeAmountsLib.sol (L41-46)
```text
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
        uint256 settlementFee =
            IMidnight(midnight).settlementFee(id, UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp));
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        return
            offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

**File:** src/periphery/MidnightBundles.sol (L246-246)
```text
    /// @dev Reverts if TakeAmountsLib or ConsumableUnitsLib reverts.
```

**File:** src/periphery/MidnightBundles.sol (L269-275)
```text
        for (uint256 i; i < collateralSupplies.length; i++) {
            address token = market.collateralParams[collateralSupplies[i].collateralIndex].token;
            pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
            forceApproveMax(token, MIDNIGHT);
            IMidnight(MIDNIGHT)
                .supplyCollateral(market, collateralSupplies[i].collateralIndex, collateralSupplies[i].assets, taker);
        }
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

**File:** test/TakeAmountsTest.sol (L47-47)
```text
        offer.buy = false;
```

**File:** test/TakeAmountsTest.sol (L70-74)
```text
    function _maxTick(uint256 settlementFee) internal pure returns (uint256) {
        uint256 maxPrice = WAD - settlementFee;
        uint256 t = TickLib.priceToTick(maxPrice, 1);
        return TickLib.tickToPrice(t) > maxPrice ? t - 1 : t;
    }
```
