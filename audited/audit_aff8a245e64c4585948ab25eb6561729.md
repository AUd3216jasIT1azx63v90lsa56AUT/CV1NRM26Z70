Now I have all the information needed. Let me trace the exact code path carefully.

### Title
Front-Running Partial Fill Causes Exact-Equality DoS in `buyWithAssetsTargetAndWithdrawCollateral` - (File: src/periphery/MidnightBundles.sol)

### Summary

`buyWithAssetsTargetAndWithdrawCollateral` enforces an exact equality `filledBuyerAssets == targetFilledBuyerAssets` at line 224. The `unitsToTake` per offer is capped by `ConsumableUnitsLib.consumableUnits`, which reads live on-chain `consumed[maker][group]` state at execution time. Any third party who takes even a minimal amount from the target offer before the bundle executes will reduce `consumableUnits`, causing the bundle to underfill and revert with `OutOfOffers`. The same flaw exists in `supplyCollateralAndSellWithAssetsTarget` at line 303.

### Finding Description

**Consumed tracking in `Midnight.sol` lines 367–369:**
When `offer.maxAssets > 0`, each `take()` increments `consumed[offer.maker][offer.group]` by the assets transacted (sellerAssets for sell offers, buyerAssets for buy offers):

```solidity
newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
require(newConsumed <= offer.maxAssets, ConsumedAssets());
``` [1](#0-0) 

**`ConsumableUnitsLib.consumableUnits` lines 14–23:**
For a sell offer with `maxAssets > 0`, the remaining capacity is computed as:
```solidity
uint256 consumed = IMidnight(midnight).consumed(offer.maker, offer.group);
// ...
return TakeAmountsLib.sellerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
```
This reads live state with no snapshot. Any increase in `consumed` between bundle submission and execution directly reduces the returned unit count. [2](#0-1) 

**Bundle loop and exact equality check in `MidnightBundles.sol` lines 208–224:**
```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.buyerAssetsToUnits(..., targetFilledBuyerAssets - filledBuyerAssets),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)  // live state
);
try IMidnight(MIDNIGHT).take(..., unitsToTake, ...) returns (uint256 resBuyerAssets, uint256) {
    filledBuyerAssets += resBuyerAssets;
} catch {}
// ...
require(filledBuyerAssets == targetFilledBuyerAssets, OutOfOffers());
``` [3](#0-2) 

**Exploit flow:**
1. Victim submits `buyWithAssetsTargetAndWithdrawCollateral` with a single sell offer O (`maxAssets` set), `targetFilledBuyerAssets = X`.
2. Attacker front-runs with `Midnight.take(O, ..., smallUnits, ...)`, consuming Y assets from offer O. This is permissionless — any address can take any offer.
3. `consumed[O.maker][O.group]` increases by Y.
4. Victim's bundle executes: `consumableUnits(O)` = `sellerAssetsToUnits(maxAssets - Y)` < full capacity.
5. `unitsToTake` is capped at the reduced value; `take` succeeds but `resBuyerAssets < targetFilledBuyerAssets`.
6. Loop ends (no more offers); `filledBuyerAssets < targetFilledBuyerAssets`.
7. `require(filledBuyerAssets == targetFilledBuyerAssets, OutOfOffers())` reverts.

**Why existing checks do not stop it:**
- The `try/catch` only handles reverts from `take`; it does not handle the case where `take` succeeds but fills fewer assets than needed.
- There is no slippage tolerance or minimum-fill threshold on the exact equality check.
- There is no snapshot of `consumed` at bundle submission time.
- The attacker's `take` is fully valid and passes all core checks (`ConsumedAssets`, `OfferExpired`, etc.).

The identical pattern exists in `supplyCollateralAndSellWithAssetsTarget` at line 303: [4](#0-3) 

### Impact Explanation

Any unprivileged actor holding loan tokens can repeatedly grief a victim's `buyWithAssetsTargetAndWithdrawCollateral` (or `supplyCollateralAndSellWithAssetsTarget`) bundle by front-running with a minimal partial fill of the target offer. The victim's transaction reverts with `OutOfOffers` every time, permanently blocking the bundle from executing as long as the attacker repeats the front-run. The victim's tokens are returned (the pull is refunded on revert), but the victim cannot complete the intended market action via the bundle.

### Likelihood Explanation

**Preconditions:**
- Victim uses `buyWithAssetsTargetAndWithdrawCollateral` with an offer that has `maxAssets > 0`.
- The victim's `takes` array has insufficient fallback offers to absorb the capacity reduction (e.g., a single-offer bundle, or the attacker front-runs all listed offers).
- Attacker has loan tokens to spend on a minimal take.

**Feasibility:** High. Taking an offer is permissionless. The attacker needs only a small amount of loan tokens (even 1 unit of sellerAssets) to reduce `consumableUnits` by at least 1, which is sufficient to break the exact equality. The attacker receives a market position (credit) in return, so the net cost is gas plus any spread, not the full loan amount. The attack is repeatable every block.

### Recommendation

Replace the exact equality check with a minimum-fill check and refund any unspent tokens to the caller:

```solidity
// Instead of:
require(filledBuyerAssets == targetFilledBuyerAssets, OutOfOffers());

// Use:
require(filledBuyerAssets >= targetFilledBuyerAssets, OutOfOffers());
uint256 refund = targetBuyerAssets - filledBuyerAssets - referralFeeAssets;
if (refund > 0) SafeTransferLib.safeTransfer(loanToken, msg.sender, refund);
```

Alternatively, accept a `minFilledBuyerAssets` parameter (analogous to `minUnits`) and allow partial fills, refunding the difference. This eliminates the exact-equality invariant that makes the bundle vulnerable to any capacity change between submission and execution. Apply the same fix to `supplyCollateralAndSellWithAssetsTarget`.

### Proof of Concept

**Foundry stateful fuzz test plan:**

```solidity
function testFrontRunDoSBuyWithAssetsTarget() public {
    // Setup: sell offer with maxAssets = 1000e18, single offer in bundle
    offer.buy = false;
    offer.maker = borrower;
    offer.maxAssets = 1000e18;
    offer.maxUnits = 0;
    collateralize(market, borrower, largeUnits);

    uint256 targetBuyerAssets = 500e18;
    Take[] memory takes = new Take[](1);
    takes[0] = Take({offer: offer, units: type(uint256).max, ratifierData: hex""});

    // Attacker front-runs: takes 1 wei of sellerAssets from the offer
    vm.prank(attacker);
    midnight.take(offer, hex"", 1, attacker, address(0), address(0), "");

    // Assert: consumed[borrower][group] > 0 now
    assertGt(midnight.consumed(borrower, offer.group), 0);

    // Victim's bundle now reverts
    vm.prank(lender);
    vm.expectRevert(IMidnightBundles.OutOfOffers.selector);
    midnightBundles.buyWithAssetsTargetAndWithdrawCollateral(
        targetBuyerAssets, 0, lender, _noPermit(), takes,
        new CollateralWithdrawal[](0), address(0), 0, address(0)
    );
}
```

**Expected assertions:**
- `midnight.consumed(borrower, offer.group) > 0` after attacker's take.
- `midnightBundles.buyWithAssetsTargetAndWithdrawCollateral(...)` reverts with `OutOfOffers`.
- Without the attacker's front-run, the same bundle call succeeds.

### Citations

**File:** src/Midnight.sol (L367-369)
```text
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
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

**File:** src/periphery/MidnightBundles.sol (L208-224)
```text
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

        require(filledBuyerAssets == targetFilledBuyerAssets, OutOfOffers());
```

**File:** src/periphery/MidnightBundles.sol (L303-303)
```text
        require(filledSellerAssets == targetFilledSellerAssets, OutOfOffers());
```
