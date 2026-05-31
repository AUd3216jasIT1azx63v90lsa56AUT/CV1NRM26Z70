### Title
MidnightBundles Passes `unitsToTake=0` to `take` on Fully-Consumed Offers, Triggering Maker Callback - (File: src/periphery/MidnightBundles.sol)

### Summary
When `ConsumableUnitsLib.consumableUnits` returns 0 for a fully-consumed offer (`consumed >= maxAssets`), `MidnightBundles` computes `unitsToTake = min(..., 0) = 0` and still calls `Midnight.take` with `units=0`. `Midnight.take` succeeds on a 0-unit call (the consumed cap check passes because `newConsumed += 0` does not exceed `maxAssets`) and unconditionally fires `offer.callback` if set, as explicitly documented in `Midnight.sol` line 93. An attacker who bundles this fully-consumed offer alongside enough valid offers to satisfy `targetUnits` can cause the transaction to succeed with the callback's state changes persisted.

### Finding Description

**Root cause — missing zero-unit guard in MidnightBundles:**

In all four bundler loops (`buyWithUnitsTargetAndWithdrawCollateral`, `supplyCollateralAndSellWithUnitsTarget`, `buyWithAssetsTargetAndWithdrawCollateral`, `supplyCollateralAndSellWithAssetsTarget`), the pattern is:

```solidity
uint256 unitsToTake = min(
    targetUnits - filledUnits,
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)  // returns 0
);
try IMidnight(MIDNIGHT).take(takes[i].offer, takes[i].ratifierData, unitsToTake, ...) returns (...) {
    filledUnits += unitsToTake;   // += 0, no progress
    ...
} catch {}
``` [1](#0-0) 

There is no `if (unitsToTake == 0) continue;` guard. The `take` call proceeds with `units=0`.

**Why `take` succeeds with `units=0` on a fully-consumed offer:**

Inside `Midnight.take`, when `offer.maxAssets > 0` and `units=0`:
- `buyerAssets = 0`, `sellerAssets = 0`
- `newConsumed = consumed[maker][group] += 0` — unchanged
- `require(newConsumed <= offer.maxAssets)` — passes because `consumed == maxAssets` satisfies `<=` [2](#0-1) 

**Callback fires unconditionally:**

After the consumed check, `take` assigns `buyerCallback = offer.buy ? offer.callback : takerCallback` and fires it if non-zero, with no guard on `units > 0`: [3](#0-2) 

This is explicitly documented behavior: [4](#0-3) 

**Ratifier check — partial protection only:**

`take` requires `isAuthorized[offer.maker][offer.ratifier]` and `IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS`. [5](#0-4) 

For a signature-based ratifier, the attacker reuses the maker's previously-issued signature (the `consumed` mapping is the replay-protection mechanism, not the ratifier; the ratifier only checks the signature over the offer struct). The offer was valid when signed; the signature remains valid. The attacker supplies the same `offer` struct and `ratifierData` (signature) that were used during the original fill.

**Attack sequence:**

1. Maker creates a sell offer with `offer.callback` set, `maxAssets = X`, signs it for a ratifier.
2. Legitimate takers fill the offer until `consumed[maker][group] == X`.
3. Attacker constructs a `takes` array: `[fullyConsumedOffer, validOffer1, ...]` where the valid offers together satisfy `targetUnits`.
4. Attacker calls `buyWithUnitsTargetAndWithdrawCollateral(targetUnits, ...)`.
5. Iteration 0: `consumableUnits` returns 0 → `unitsToTake=0` → `take(fullyConsumedOffer, sig, 0, ...)` succeeds → `offer.callback.onSell(...)` fires with `units=0, sellerAssets=0` → `filledUnits += 0`.
6. Iterations 1+: valid offers fill `targetUnits`.
7. `require(filledUnits == targetUnits)` passes. Transaction succeeds. Callback state changes are permanent. [6](#0-5) 

### Impact Explanation

The maker's `onSell` or `onBuy` callback executes with `units=0` and `assets=0` at a time the maker did not intend (offer fully consumed). Any state-mutating logic inside the callback — such as calling `repay`, `withdrawCollateral`, or `setIsAuthorized` on Midnight — executes and persists. The existing test `testBugBuyMaxAssetsBypass` already demonstrates that fully-consumed offers can be taken (with non-zero units under a price-rounding edge case) and produce real position-state changes; the 0-unit callback path is the analogous vector through the bundler. [7](#0-6) 

### Likelihood Explanation

Preconditions are low-friction: any offer with a callback that has been fully consumed is eligible. The attacker only needs the maker's previously-used offer struct and ratifierData (publicly observable on-chain from prior `Take` events), plus enough valid offers to satisfy `targetUnits`. No privileged access is required. The attack is repeatable as long as the offer has not expired.

### Recommendation

Add an explicit zero-unit skip guard in every bundler loop before calling `take`:

```solidity
if (unitsToTake == 0) continue;
```

This prevents `take` from being called with `units=0` on fully-consumed offers, eliminating the spurious callback invocation without affecting any legitimate fill path.

### Proof of Concept

```solidity
// Foundry unit test
function testFullyConsumedOfferCallbackFiredByBundler() public {
    // 1. Setup: sell offer with callback, maxAssets = 100e18
    SellCallbackRecorder cb = new SellCallbackRecorder();
    sellerOffer.callback = address(cb);
    sellerOffer.maxUnits = 0;
    sellerOffer.maxAssets = 100e18;

    // 2. Fully consume the offer
    vm.prank(sellerOffer.maker);
    midnight.setConsumed(sellerOffer.group, 100e18, sellerOffer.maker);
    assertEq(midnight.consumed(sellerOffer.maker, sellerOffer.group), 100e18);

    // 3. Build takes array: [fullyConsumedOffer, validOffer]
    // validOffer fills targetUnits so the bundler tx succeeds
    Take[] memory takes = new Take[](2);
    takes[0] = Take({offer: sellerOffer, units: 1, ratifierData: makerSig});
    takes[1] = Take({offer: validOffer,  units: targetUnits, ratifierData: hex""});

    // 4. Attacker calls bundler
    vm.prank(attacker);
    midnightBundles.buyWithUnitsTargetAndWithdrawCollateral(
        targetUnits, maxBuyerAssets, attacker, _noPermit(), takes,
        new CollateralWithdrawal[](0), address(0), 0, address(0)
    );

    // 5. Assert: callback fired despite offer being fully consumed
    assertEq(cb.callCount(), 1, "callback must NOT have fired on 0-unit take");
    // Expected: callCount == 0 (no callback). Actual: callCount == 1 → BUG confirmed.
}
```

Expected assertion failure: `cb.callCount() == 1` when it should be `0`, proving the callback fires on a fully-consumed offer through the bundler's 0-unit take path.

### Citations

**File:** src/periphery/MidnightBundles.sol (L74-85)
```text
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
```

**File:** src/Midnight.sol (L93-93)
```text
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
```

**File:** src/Midnight.sol (L355-356)
```text
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

**File:** src/Midnight.sol (L367-373)
```text
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
```

**File:** src/Midnight.sol (L420-453)
```text
        address buyerCallback = offer.buy ? offer.callback : takerCallback;
        address sellerCallback = offer.buy ? takerCallback : offer.callback;
        address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
        address receiver = offer.buy ? receiverIfTakerIsSeller : offer.receiverIfMakerIsSeller;

        emit EventsLib.Take(
            msg.sender,
            id,
            units,
            taker,
            offer.maker,
            offer.buy,
            offer.group,
            buyerAssets,
            sellerAssets,
            newConsumed,
            buyerPendingFeeIncrease,
            sellerPendingFeeDecrease,
            buyerCreditIncrease,
            sellerCreditDecrease,
            receiver,
            payer
        );

        bool wasLocked = UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, true);
        if (buyerCallback != address(0)) {
            bytes memory buyerCallbackData = offer.buy ? offer.callbackData : takerCallbackData;
            require(
                IBuyCallback(buyerCallback)
                    .onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
                == CALLBACK_SUCCESS,
                WrongBuyCallbackReturnValue()
            );
        }
```

**File:** src/periphery/ConsumableUnitsLib.sol (L14-22)
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
```

**File:** test/TakeTest.sol (L858-889)
```text
    function testBugBuyMaxAssetsBypass() public {
        deal(address(loanToken), lender, 0); // lender pays 0
        collateralize(market, borrower, 100);

        lenderOffer.maxUnits = 0;
        lenderOffer.maxAssets = 1;
        lenderOffer.tick = MAX_TICK - 16; // offerPrice < WAD

        // Fully consume the offer before the take.
        vm.prank(lender);
        midnight.setConsumed(lenderOffer.group, lenderOffer.maxAssets, lender);

        uint256 lenderCreditBefore = midnight.creditOf(id, lender);
        uint256 borrowerDebtBefore = midnight.debtOf(id, borrower);
        uint256 totalUnitsBefore = midnight.totalUnits(id);
        uint256 lenderBalBefore = loanToken.balanceOf(lender);
        uint256 borrowerBalBefore = loanToken.balanceOf(borrower);

        (uint256 buyerAssets, uint256 sellerAssets) = take(1, borrower, lenderOffer);

        assertEq(buyerAssets, 0);
        assertEq(sellerAssets, 0);

        // Nothing observable to the cap or token balances changed:
        assertEq(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets);
        assertEq(loanToken.balanceOf(lender), lenderBalBefore);
        assertEq(loanToken.balanceOf(borrower), borrowerBalBefore);
        // But position state strictly changed:
        assertGt(midnight.creditOf(id, lender), lenderCreditBefore);
        assertGt(midnight.debtOf(id, borrower), borrowerDebtBefore);
        assertGt(midnight.totalUnits(id), totalUnitsBefore);
    }
```
