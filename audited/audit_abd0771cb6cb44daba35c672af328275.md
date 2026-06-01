### Title
Uncaught `buyerAssetsToUnits` Arithmetic Underflow via tick=0 Buy Offer DoSes All Bundle Sell Flows - (File: src/periphery/ConsumableUnitsLib.sol)

### Summary

`TickLib.tickToPrice(0)` returns exactly `0`. When a buy offer is placed at `tick=0` with `maxUnits=0`, `ConsumableUnitsLib.consumableUnits` routes into `TakeAmountsLib.buyerAssetsToUnits`, which performs the unchecked subtraction `offerPrice - settlementFee` under Solidity 0.8 checked arithmetic. With any non-zero settlement fee, this underflows and reverts. Because `consumableUnits` is called **outside** the `try/catch` block in every bundle sell function, the revert propagates and aborts the entire bundle transaction.

### Finding Description

**Exact code path:**

`TickLib.tickToPrice(0)` is proven to return `0`: [1](#0-0) [2](#0-1) 

`ConsumableUnitsLib.consumableUnits` branches into `buyerAssetsToUnits` when `offer.maxUnits == 0` and `offer.buy == true`: [3](#0-2) 

Inside `TakeAmountsLib.buyerAssetsToUnits`, line 26 performs the subtraction without an `unchecked` block: [4](#0-3) 

With `offerPrice = 0` and `settlementFee > 0`, `offerPrice - settlementFee` underflows under Solidity 0.8 checked arithmetic and reverts.

**Why the try/catch does not save the bundle:**

In both `supplyCollateralAndSellWithUnitsTarget` and `supplyCollateralAndSellWithAssetsTarget`, `ConsumableUnitsLib.consumableUnits` is called at line 150 / 290 **before** the `try IMidnight(MIDNIGHT).take(...)` block. The try/catch only wraps the `take` call itself: [5](#0-4) [6](#0-5) 

The NatSpec explicitly acknowledges this gap: "Reverts if ConsumableUnitsLib reverts": [7](#0-6) [8](#0-7) 

**Attacker-controlled inputs:**

- `offer.buy = true` — freely set by the maker
- `offer.tick = 0` — always accessible because `0 % tickSpacing == 0` for any `tickSpacing`
- `offer.maxUnits = 0`, `offer.maxAssets > 0` — freely set by the maker; this is the branch condition that routes into `buyerAssetsToUnits`

**Existing protections reviewed and insufficient:**

`Midnight.take` itself also reverts on `offerPrice < settlementFee` (line 361), but that revert is inside the try/catch and would be silently skipped. The problem is that `consumableUnits` fires first, outside the try/catch, so its revert is never caught: [9](#0-8) 

### Impact Explanation

Any call to `supplyCollateralAndSellWithUnitsTarget` or `supplyCollateralAndSellWithAssetsTarget` that includes the crafted buy offer in its `takes` array will revert entirely. This DoSes all legitimate takers (borrowers) who attempt to sell units against a bundle of buy offers whenever the attacker's offer is present. The attacker's cost is only the gas to publish the offer; no capital is at risk because the offer itself can never be successfully taken (it would revert in `Midnight.take` too), but its mere presence in a bundle is enough to abort the whole transaction.

### Likelihood Explanation

Preconditions are trivially achievable: any unprivileged maker can publish a buy offer at `tick=0` with `maxUnits=0` and `maxAssets > 0`. Any market with a non-zero settlement fee (the default for all created markets) satisfies `settlementFee > 0`. The attack is repeatable at negligible cost and requires no special permissions. The attacker does not need to be the taker or have any position.

### Recommendation

Wrap the `consumableUnits` call in a try/catch in every bundle loop, and skip the offer (returning 0 units) if it reverts — mirroring the existing treatment of `take` failures. Alternatively, add a guard in `buyerAssetsToUnits` (and `sellerAssetsToUnits`) that returns 0 instead of reverting when `offerPrice < settlementFee` for a buy offer, since 0 consumable units is the semantically correct answer (the offer cannot be taken at all).

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {MidnightBundles} from "src/periphery/MidnightBundles.sol";
import {ConsumableUnitsLib} from "src/periphery/ConsumableUnitsLib.sol";
import {TakeAmountsLib} from "src/periphery/TakeAmountsLib.sol";
import {TickLib} from "src/libraries/TickLib.sol";
// ... standard BaseTest imports

contract DosConsumableUnitsTest is BaseTest {
    function testDosViaTickZeroBuyOffer() public {
        // 1. Confirm tickToPrice(0) == 0
        assertEq(TickLib.tickToPrice(0), 0);

        // 2. Set a non-zero settlement fee on the market
        midnight.setMarketSettlementFee(id, 0, 1e12); // any non-zero value
        uint256 fee = midnight.settlementFee(id, market.maturity - block.timestamp);
        assertGt(fee, 0);

        // 3. Attacker crafts a buy offer at tick=0, maxUnits=0, maxAssets=1e18
        Offer memory attackOffer;
        attackOffer.buy = true;
        attackOffer.tick = 0;
        attackOffer.maxUnits = 0;
        attackOffer.maxAssets = 1e18;
        attackOffer.maker = attacker;
        attackOffer.market = market;
        attackOffer.expiry = block.timestamp + 1 days;
        attackOffer.ratifier = address(openRatifier);

        // 4. Build a bundle that includes the crafted offer
        Take[] memory takes = new Take[](1);
        takes[0] = Take({offer: attackOffer, ratifierData: "", units: 1e18});

        // 5. Victim calls supplyCollateralAndSellWithUnitsTarget — expect full revert
        vm.expectRevert(); // arithmetic underflow in buyerAssetsToUnits
        vm.prank(victim);
        bundles.supplyCollateralAndSellWithUnitsTarget(
            1e18, 0, victim, victim, new CollateralSupply[](0), takes, 0, address(0)
        );

        // Assertion: the entire bundle reverted, not just the individual take
    }
}
```

Expected: the call reverts with an arithmetic underflow (panic 0x11) originating from `TakeAmountsLib.buyerAssetsToUnits` line 26, propagating through `ConsumableUnitsLib.consumableUnits` and aborting the bundle before any `take` is attempted.

### Citations

**File:** src/libraries/TickLib.sol (L44-52)
```text
    function tickToPrice(uint256 tick) internal pure returns (uint256) {
        require(tick <= MAX_TICK, TickOutOfRange());
        unchecked {
            // forge-lint: disable-next-item(unsafe-typecast)
            return uint256(1e36)
                    .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
                    .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
        }
    }
```

**File:** test/TickLibTest.sol (L15-17)
```text
    function testTickToPriceMinMax() public pure {
        assertEq(TickLib.tickToPrice(0), 0, "tick 0");
        assertEq(TickLib.tickToPrice(2), 1e12, "first non-zero tick");
```

**File:** src/periphery/ConsumableUnitsLib.sol (L16-22)
```text
        if (offer.maxUnits > 0) {
            return offer.maxUnits.zeroFloorSub(consumed);
        } else if (offer.buy) {
            return TakeAmountsLib.buyerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
        } else {
            return TakeAmountsLib.sellerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
        }
```

**File:** src/periphery/TakeAmountsLib.sol (L22-27)
```text
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
        uint256 settlementFee =
            IMidnight(midnight).settlementFee(id, UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp));
        // Mirrors Midnight's computation to revert if offerPrice < settlementFee in case of a buy offer.
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + settlementFee;
```

**File:** src/periphery/MidnightBundles.sol (L110-112)
```text
    /// @dev Skips every reason why take can revert (including ones that are not asynchrony related).
    /// @dev Reverts if ConsumableUnitsLib reverts.
    /// @dev If taking an offer reverts, the bundler will completely skip this offer.
```

**File:** src/periphery/MidnightBundles.sol (L147-161)
```text
            uint256 unitsToTake = min(
                targetUnits - filledUnits,
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

**File:** src/periphery/MidnightBundles.sol (L174-176)
```text
    /// @dev Skips every reason why take can revert (including ones that are not asynchrony related).
    /// @dev Reverts if TakeAmountsLib or ConsumableUnitsLib reverts.
    /// @dev If taking an offer reverts, the bundler will completely skip this offer.
```

**File:** src/periphery/MidnightBundles.sol (L285-301)
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
        }
```

**File:** src/Midnight.sol (L358-362)
```text
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
        uint256 _settlementFee = settlementFee(id, timeToMaturity);
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
```
