### Title
Division-by-Zero in `sellerAssetsToUnits` When `tickToPrice(tick) == settlementFee` Causes Unrecoverable DoS of `supplyCollateralAndSellWithAssetsTarget` - (File: src/periphery/TakeAmountsLib.sol)

### Summary

`sellerAssetsToUnits` computes `sellerPrice = offerPrice - settlementFee` for a buy offer and then calls `mulDivUp(targetSellerAssets, WAD, sellerPrice)`. When `offerPrice == settlementFee`, `sellerPrice` is exactly `0`, and `mulDivUp` reverts due to underflow at `(d - 1)` with `d = 0`. Critically, `Midnight.take()` accepts this state without reverting (it simply yields `sellerAssets = 0`), so the invariant stated in the NatDoc — "midnight reverts too" — is false for the equality case. Because `supplyCollateralAndSellWithAssetsTarget` calls `sellerAssetsToUnits` outside its `try/catch` block, a single attacker-controlled offer at the matching tick permanently reverts the entire bundler call for any victim who includes it.

### Finding Description

**Root cause — `sellerAssetsToUnits` (TakeAmountsLib.sol lines 41–46):**

```solidity
uint256 offerPrice = TickLib.tickToPrice(offer.tick);
uint256 settlementFee =
    IMidnight(midnight).settlementFee(id, UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp));
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

When `offer.buy == true` and `offerPrice == settlementFee`, `sellerPrice = 0`. `mulDivUp` is:

```solidity
function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y + (d - 1)) / d;   // d=0 → (d-1) underflows → revert
}
```

Solidity 0.8 checked arithmetic causes `(d - 1)` to underflow and revert when `d = 0`.

**Why `take()` does NOT revert in the same state (Midnight.sol lines 361–364):**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;  // = 0, no underflow
uint256 buyerPrice  = sellerPrice + _settlementFee;                           // = settlementFee
uint256 buyerAssets = units.mulDivDown(buyerPrice, WAD);                      // fine
uint256 sellerAssets = units.mulDivDown(sellerPrice, WAD);                    // = 0, fine
```

`mulDivDown` is `(x * y) / d` — dividing by `WAD` (1e18), not by `sellerPrice`. No revert.

**Exploit path in `supplyCollateralAndSellWithAssetsTarget` (MidnightBundles.sol lines 285–300):**

```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(   // ← OUTSIDE try/catch; reverts here
        MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
    ),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
);
try IMidnight(MIDNIGHT).take(...) returns (...) { ... } catch {}  // never reached
```

The `sellerAssetsToUnits` call is not wrapped in the `try/catch`. Its revert propagates to the caller unconditionally.

**Attacker inputs:**
- Attacker is an unprivileged maker who creates a buy offer at tick `T` where `TickLib.tickToPrice(T) == settlementFee(id, ttm)`.
- Both `tickToPrice` output and settlement fees are rounded to multiples of `PRICE_ROUNDING_STEP = 1e12`, so exact equality is achievable: the attacker reads the current settlement fee (a public view), calls `TickLib.priceToTick(settlementFee, tickSpacing)` to find the matching tick, and posts the offer.
- No privileged access is required; any maker can post a buy offer at any valid tick.

**Existing protections reviewed and found insufficient:**
- The NatDoc comment `"Reverts if offerPrice < settlementFee in case of a buy offer (midnight reverts too)"` is incorrect for the equality case: midnight does not revert, but `sellerAssetsToUnits` does.
- `buyerAssetsToUnits` avoids this because it divides by `buyerPrice = sellerPrice + settlementFee = settlementFee > 0`, not by `sellerPrice`.
- There is no `require(sellerPrice > 0)` guard in `sellerAssetsToUnits`.
- The `try/catch` in `supplyCollateralAndSellWithAssetsTarget` only covers `IMidnight.take()`, not the preceding `sellerAssetsToUnits` call.

### Impact Explanation

Any call to `supplyCollateralAndSellWithAssetsTarget` that includes a buy offer at the settlement-fee price point in its `takes[]` array reverts entirely. The attacker posts one such offer (low cost: just a transaction), and any victim taker who routes through that offer — e.g., via an aggregator or UI that enumerates open buy offers — has their entire bundler transaction reverted. This blocks the sell-via-periphery path for affected takers for as long as the offer exists and the settlement fee remains at that level.

### Likelihood Explanation

- Settlement fees are public and readable on-chain; the attacker can compute the exact matching tick trivially using `TickLib.priceToTick`.
- Both tick prices and settlement fees are multiples of `1e12`, so exact equality is structurally guaranteed to be reachable for any non-zero settlement fee within the tick range.
- The attacker bears only the gas cost of posting one buy offer; no capital is at risk.
- The DoS persists until the offer is cancelled (by the attacker, who has no incentive to do so) or the settlement fee changes to a value with no matching tick.
- The attack is repeatable: if the fee changes, the attacker posts a new offer at the new matching tick.

### Recommendation

Add a zero-guard in `sellerAssetsToUnits` for the case where `sellerPrice == 0`:

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
if (sellerPrice == 0) return type(uint256).max; // no finite units can yield positive sellerAssets
return offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

Returning `type(uint256).max` causes `min(...)` in `supplyCollateralAndSellWithAssetsTarget` to select `takes[i].units` or `consumableUnits` instead, and the subsequent `take()` call (which succeeds) returns `sellerAssets = 0`, so the offer is effectively skipped and the loop continues. Alternatively, wrap the `sellerAssetsToUnits` call in a `try/catch` inside `supplyCollateralAndSellWithAssetsTarget` and skip the offer on revert, consistent with how `take()` failures are already handled.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {MidnightBundles, Take, CollateralSupply} from "src/periphery/MidnightBundles.sol";
import {TakeAmountsLib} from "src/periphery/TakeAmountsLib.sol";
import {TickLib} from "src/libraries/TickLib.sol";
import {IMidnight, Offer} from "src/interfaces/IMidnight.sol";

contract SellerAssetsToUnitsDivZeroTest is /* base test setup */ {
    function testSellerAssetsToUnitsRevertsWhenPriceEqualsSettlementFee() public {
        // 1. Touch market to get current settlement fee
        bytes32 id = midnight.touchMarket(market);
        uint256 ttm = market.maturity - block.timestamp;
        uint256 fee = midnight.settlementFee(id, ttm);
        require(fee > 0, "need nonzero fee");

        // 2. Find tick T where tickToPrice(T) == fee exactly
        uint256 tick = TickLib.priceToTick(fee, marketTickSpacing);
        require(TickLib.tickToPrice(tick) == fee, "no exact tick match");

        // 3. Attacker posts buy offer at tick T
        Offer memory attackerOffer = /* build buy offer at tick */ ;
        attackerOffer.tick = tick;
        attackerOffer.buy = true;

        // 4. Assert: sellerAssetsToUnits reverts (division by zero)
        vm.expectRevert(); // arithmetic underflow in mulDivUp(WAD, 0)
        TakeAmountsLib.sellerAssetsToUnits(address(midnight), id, attackerOffer, 1e18);

        // 5. Assert: take() with computed units=1 does NOT revert (sellerAssets=0)
        collateralize(market, attacker, 1);
        (uint256 buyerAssets, uint256 sellerAssets) = midnight.take(
            attackerOffer, hex"", 1, victim, victim, address(0), ""
        );
        assertEq(sellerAssets, 0, "take succeeds with sellerAssets=0");

        // 6. Assert: supplyCollateralAndSellWithAssetsTarget reverts when offer included
        Take[] memory takes = new Take[](1);
        takes[0] = Take({offer: attackerOffer, units: type(uint256).max, ratifierData: hex""});
        vm.prank(victim);
        vm.expectRevert(); // propagated from sellerAssetsToUnits outside try/catch
        midnightBundles.supplyCollateralAndSellWithAssetsTarget(
            1e18, type(uint256).max, victim, victim,
            new CollateralSupply[](0), takes, 0, address(0)
        );
    }
}
```

**Expected assertions:**
- Step 4 reverts (confirms the division-by-zero in `sellerAssetsToUnits`).
- Step 5 succeeds with `sellerAssets == 0` (confirms `take()` is unaffected, disproving the NatDoc claim).
- Step 6 reverts (confirms the DoS on `supplyCollateralAndSellWithAssetsTarget`).

---

**Key code references:**

`sellerAssetsToUnits` missing zero-guard: [1](#0-0) 

`mulDivUp` reverts when `d=0` due to checked `(d-1)`: [2](#0-1) 

`take()` accepts `sellerPrice=0` without reverting: [3](#0-2) 

`sellerAssetsToUnits` called outside `try/catch` in bundler: [4](#0-3) 

Tick prices and settlement fees both rounded to `1e12`, enabling exact equality: [5](#0-4) [6](#0-5)

### Citations

**File:** src/periphery/TakeAmountsLib.sol (L44-46)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        return
            offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

**File:** src/libraries/UtilsLib.sol (L34-36)
```text
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
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

**File:** src/libraries/TickLib.sol (L8-8)
```text
uint256 constant PRICE_ROUNDING_STEP = 1e12;
```

**File:** src/libraries/ConstantsLib.sol (L11-17)
```text
uint256 constant MAX_SETTLEMENT_FEE_0_DAYS = 0.000014e18;
uint256 constant MAX_SETTLEMENT_FEE_1_DAY = 0.000014e18;
uint256 constant MAX_SETTLEMENT_FEE_7_DAYS = 0.000098e18;
uint256 constant MAX_SETTLEMENT_FEE_30_DAYS = 0.000417e18;
uint256 constant MAX_SETTLEMENT_FEE_90_DAYS = 0.00125e18;
uint256 constant MAX_SETTLEMENT_FEE_180_DAYS = 0.0025e18;
uint256 constant MAX_SETTLEMENT_FEE_360_DAYS = 0.005e18;
```
