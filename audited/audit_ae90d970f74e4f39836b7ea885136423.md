Now I have all the information needed for a complete analysis. Let me trace the exact code paths.

**Key facts established:**

1. `mulDivUp(x, y, d)` is `(x * y + (d - 1)) / d` — when `d = 0`, `d - 1` underflows (uint256), causing an arithmetic revert. Confirmed by `testMulDivUpDivisionByZero`.

2. In `sellerAssetsToUnits` for a buy offer: `sellerPrice = offerPrice - settlementFee`. If `offerPrice == settlementFee`, `sellerPrice = 0`, and `mulDivUp(targetSellerAssets, WAD, 0)` reverts.

3. In Midnight's `take()` with `sellerPrice = 0`: `sellerAssets = units.mulDivDown(0, WAD) = 0` — this **succeeds**.

4. `MAX_SETTLEMENT_FEE_0_DAYS = 0.000014e18 = 14e12`. Price quantization near maturity is `1e-6 = 1e12`. So ticks exist where `tickToPrice(tick) ∈ {1e12, 2e12, ..., 14e12}`, making `offerPrice == settlementFee(id, 0)` achievable.

5. `sellerAssetsToUnits` is called **outside** the `try/catch` in `supplyCollateralAndSellWithAssetsTarget` (line 286), so its revert propagates to the caller.

6. The `NoDivisionByZero.spec` Certora spec only covers `Midnight.sol`, not the periphery library.

---

### Title
`sellerAssetsToUnits` division-by-zero when `offerPrice == settlementFee` for post-maturity buy offer DoS - (`src/periphery/TakeAmountsLib.sol`)

### Summary
`TakeAmountsLib.sellerAssetsToUnits` computes `sellerPrice = offerPrice - settlementFee` for buy offers and then calls `mulDivUp(targetSellerAssets, WAD, sellerPrice)`. When `offerPrice == settlementFee(id, 0)` post-maturity, `sellerPrice = 0` and `mulDivUp` reverts via uint256 underflow in `d - 1`. Midnight's core `take()` handles this case correctly (returning `sellerAssets = 0`), so the periphery diverges from core and `supplyCollateralAndSellWithAssetsTarget` permanently reverts for any `takes` array containing such an offer.

### Finding Description

**Root cause — `TakeAmountsLib.sellerAssetsToUnits`, lines 41–46:**

```solidity
uint256 offerPrice = TickLib.tickToPrice(offer.tick);
uint256 settlementFee =
    IMidnight(midnight).settlementFee(id, UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp));
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
``` [1](#0-0) 

Post-maturity, `zeroFloorSub(offer.market.maturity, block.timestamp) = 0`, so `settlementFee = settlementFeeCbp0 * CBP`. When `tickToPrice(offer.tick) == settlementFee`, `sellerPrice = 0`. The call `mulDivUp(targetSellerAssets, WAD, 0)` executes `(x * y + (0 - 1)) / 0`, which underflows on `0 - 1` (uint256 arithmetic), causing an arithmetic revert. [2](#0-1) 

**Why Midnight's `take()` succeeds:** In `Midnight.take()`, the forward direction computes `sellerAssets = units.mulDivDown(sellerPrice, WAD) = units.mulDivDown(0, WAD) = 0`, which is valid. The `CannotIncreaseDebtPostMaturity` check passes because `sellerDebtIncrease = 0` when the seller has enough credit. [3](#0-2) 

**Why the periphery reverts:** In `supplyCollateralAndSellWithAssetsTarget`, `sellerAssetsToUnits` is called **outside** the `try/catch` block that wraps `take()`. The comment explicitly acknowledges this: "Reverts if TakeAmountsLib or ConsumableUnitsLib reverts." [4](#0-3) 

**Exploit flow:**
1. Market passes maturity (`block.timestamp > offer.market.maturity`).
2. Attacker creates a buy offer at a tick where `tickToPrice(tick) == settlementFee(id, 0)`. With `MAX_SETTLEMENT_FEE_0_DAYS = 14e12` and price quantization of `1e12`, valid ticks exist for this condition (e.g., `settlementFeeCbp0 = 1` → `settlementFee = 1e12`, and a tick with `tickToPrice(tick) = 1e12`).
3. Attacker broadcasts this offer off-chain. Off-chain routers include it in the `takes` array for `supplyCollateralAndSellWithAssetsTarget`.
4. Victim calls `supplyCollateralAndSellWithAssetsTarget` with this offer in `takes`.
5. `sellerAssetsToUnits` is called with `targetSellerAssets > 0` and `sellerPrice = 0` → arithmetic revert.
6. The entire transaction reverts. The offer remains valid and the condition persists as long as the market is post-maturity with the same fee.

**Existing protections are insufficient:** `buyerAssetsToUnits` has a guard (`require(buyerPrice <= WAD)`) that incidentally catches `offerPrice < settlementFee` via underflow, but `sellerAssetsToUnits` has no analogous guard for `sellerPrice == 0`. [5](#0-4) 

### Impact Explanation
Any call to `supplyCollateralAndSellWithAssetsTarget` that includes a buy offer where `tickToPrice(offer.tick) == settlementFee(id, 0)` post-maturity will revert unconditionally, regardless of `targetSellerAssets`. This is a permanent DoS of the sell-via-periphery path for post-maturity markets whenever such an offer appears in the routing set. The attacker's cost is only the gas to create the offer; no funds are at risk for the attacker.

### Likelihood Explanation
**Preconditions:**
- Market is post-maturity (time-locked, inevitable for any market).
- `settlementFeeCbp0 >= 1` (any non-zero post-maturity fee, which is the common case).
- A tick exists where `tickToPrice(tick) == settlementFee(id, 0)` — achievable given price quantization of `1e12` and max fee of `14e12`.
- The attacker's offer is included in the victim's `takes` array via off-chain routing.

The condition is repeatable: the offer can be recreated after expiry, and the revert is deterministic. Any routing system that automatically aggregates available buy offers is vulnerable.

### Recommendation
Add a guard in `sellerAssetsToUnits` for the `sellerPrice == 0` case. When `sellerPrice == 0` and `targetSellerAssets > 0`, no finite number of units can yield positive seller assets, so return `type(uint256).max` (which `min(...)` will reduce to `takes[i].units` or `consumableUnits`, allowing `take()` to proceed and return `sellerAssets = 0`). When `targetSellerAssets == 0`, return `0`.

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
if (sellerPrice == 0) {
    return targetSellerAssets == 0 ? 0 : type(uint256).max;
}
return offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

This mirrors how Midnight's `take()` handles `sellerPrice = 0` (returning `sellerAssets = 0`) and avoids the divergence.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {TakeAmountsLib} from "src/periphery/TakeAmountsLib.sol";
import {MidnightBundles} from "src/periphery/MidnightBundles.sol";
// ... standard test imports

contract SellerAssetsToUnitsZeroPriceTest is BaseTest {
    function testSellerAssetsToUnitsRevertsWhenSellerPriceZero() public {
        // Setup: post-maturity market with settlementFeeCbp0 = 1 (fee = 1e12)
        market.maturity = block.timestamp - 1; // already past maturity
        id = midnight.touchMarket(market);
        midnight.setMarketSettlementFee(id, 0, 1e12); // settlementFee(id, 0) = 1e12

        // Find tick where tickToPrice(tick) == 1e12
        // (tick spacing 1, tick at price quantization boundary)
        uint256 targetTick = /* tick where tickToPrice == 1e12 */;

        Offer memory buyOffer;
        buyOffer.buy = true;
        buyOffer.tick = targetTick;
        buyOffer.market = market;
        // ... fill other fields

        // Assert: sellerAssetsToUnits reverts with arithmetic error
        vm.expectRevert(stdError.arithmeticError);
        TakeAmountsLib.sellerAssetsToUnits(address(midnight), id, buyOffer, 1e18);
    }

    function testTakeSucceedsWhenSellerPriceZero() public {
        // Same setup as above
        // Assert: take() succeeds and returns sellerAssets = 0
        (uint256 buyerAssets, uint256 sellerAssets) = midnight.take(buyOffer, ...);
        assertEq(sellerAssets, 0, "sellerAssets should be 0");
        assertGt(buyerAssets, 0, "buyerAssets should be positive");
    }

    function testSupplyCollateralAndSellRevertsWithZeroSellerPriceOffer() public {
        // Same setup; include the zero-sellerPrice buy offer in takes[]
        // Assert: supplyCollateralAndSellWithAssetsTarget reverts
        vm.expectRevert(stdError.arithmeticError);
        midnightBundles.supplyCollateralAndSellWithAssetsTarget(
            1e18, type(uint256).max, taker, receiver,
            collateralSupplies, takes, 0, address(0)
        );
    }
}
```

**Expected assertions:**
- `sellerAssetsToUnits` reverts with `stdError.arithmeticError` when `sellerPrice = 0` and `targetSellerAssets > 0`.
- `midnight.take()` with the same offer and `units > 0` succeeds and returns `sellerAssets = 0`.
- `supplyCollateralAndSellWithAssetsTarget` reverts when the zero-sellerPrice offer is in `takes`.

### Citations

**File:** src/periphery/TakeAmountsLib.sol (L25-29)
```text
        // Mirrors Midnight's computation to revert if offerPrice < settlementFee in case of a buy offer.
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + settlementFee;
        require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne());
        return offer.buy ? targetBuyerAssets.mulDivUp(WAD, buyerPrice) : targetBuyerAssets.mulDivDown(WAD, buyerPrice);
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
