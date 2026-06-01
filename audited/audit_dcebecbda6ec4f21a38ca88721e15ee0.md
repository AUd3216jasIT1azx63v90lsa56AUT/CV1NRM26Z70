### Title
Buy offer with `offerPrice < WAD` can be taken with `units > 0` after `consumed == maxAssets`, mutating position state and triggering maker callback with zero assets - (`src/Midnight.sol`)

### Summary
When a buy offer has `maxAssets > 0` and `offerPrice < WAD` (i.e., `tick < MAX_TICK`), the consumed-accounting increment uses `buyerAssets = units.mulDivDown(buyerPrice, WAD)`, which rounds down to 0 for sufficiently small `units`. An attacker can call `take()` with `units = 1` after `consumed == maxAssets`, causing `newConsumed` to remain at `maxAssets`, passing the cap check, while still mutating buyer credit, seller debt, `totalUnits`, and triggering the maker's `onBuy` callback. This is confirmed by the existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol`.

### Finding Description

**Code path — `src/Midnight.sol` lines 363–418:**

Line 363 computes `buyerAssets` for a buy offer using `mulDivDown`: [1](#0-0) 

`mulDivDown(x, y, d)` is `(x * y) / d` truncated: [2](#0-1) 

When `buyerPrice < WAD` (which holds whenever `offerPrice < WAD`, i.e., any tick below `MAX_TICK`), `mulDivDown(1, buyerPrice, WAD) = 0`.

Line 368 increments `consumed` by `buyerAssets`: [3](#0-2) 

If `consumed[maker][group]` is already equal to `maxAssets`, adding 0 leaves `newConsumed == maxAssets`, which satisfies `require(newConsumed <= offer.maxAssets)`. The function does not revert.

Execution then continues to mutate position state with the full `units = 1`: [4](#0-3) [5](#0-4) 

And the maker's `onBuy` callback is invoked with `buyerAssets = 0` and `units = 1`: [6](#0-5) 

**Attacker inputs:**
- `offer.buy = true`, `offer.maxAssets = N > 0`, `offer.tick` such that `tickToPrice(tick) < WAD`
- Pre-state: `consumed[maker][group] == N`
- `units = 1`

**Why existing checks fail:**

The `require(newConsumed <= offer.maxAssets)` check at line 369 is the only guard. It passes because `newConsumed = N + 0 = N`. There is no check that `units == 0` when `buyerAssets == 0`, and no check that `consumed == maxAssets` implies the call must revert.

The Certora spec rule `takeConsumedAtMaxUnchangedAssets` only asserts that `consumed` does not increase past `maxAssets` — it does not assert that `units` must be zero or that position state is unchanged: [7](#0-6) 

The analogous strong rule (`fullyConsumedOfferRevertsOnNonTrivialTake`) exists only for the `maxAssets == 0` (units-mode) path: [8](#0-7) 

### Impact Explanation

The existing test `testBugBuyMaxAssetsBypass` directly confirms the scoped impact: [9](#0-8) 

After `consumed == maxAssets`, a `take(units=1)` call:
- Returns `buyerAssets = 0`, `sellerAssets = 0` — no loan tokens move
- Leaves `consumed` unchanged at `maxAssets`
- But increases `creditOf(lender)`, `debtOf(borrower)`, and `totalUnits` by 1 unit each
- Triggers the maker's `onBuy` callback with `buyerAssets = 0, units = 1`

This violates the invariant that `consumed == maxAssets` means the offer is exhausted: an unprivileged taker can create unbacked credit/debt and spam the maker's callback at zero asset cost, repeatable indefinitely.

### Likelihood Explanation

**Preconditions:**
1. `offer.buy = true` — standard buy offer, no privilege required.
2. `offerPrice < WAD` — any tick below `MAX_TICK`; the test uses `MAX_TICK - 16`. This is a normal, common configuration.
3. `consumed[maker][group] == maxAssets` — reachable by a prior legitimate fill or by the maker calling `setConsumed`.
4. `units = 1` — attacker-controlled, no constraint.

All preconditions are reachable by an unprivileged taker with no special access. The attack is repeatable on every call since `consumed` never increases past `maxAssets`.

### Recommendation

Add a guard that when `offer.maxAssets > 0` and `buyerAssets == 0` (for buy offers) or `sellerAssets == 0` (for sell offers), the call must have `units == 0`, or equivalently revert if `units > 0` but the asset increment is zero:

```solidity
// After computing buyerAssets / sellerAssets, before the consumed block:
uint256 assetsDelta = offer.buy ? buyerAssets : sellerAssets;
if (offer.maxAssets > 0) {
    require(units == 0 || assetsDelta > 0, ZeroAssetTake());
    newConsumed = consumed[offer.maker][offer.group] += assetsDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

This mirrors the existing strong invariant enforced for units-mode offers and closes the rounding gap.

### Proof of Concept

The existing Foundry unit test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` (lines 857–889) is a complete PoC. Its assertions are: [10](#0-9) 

**Additional fuzz/invariant test plan:**

```solidity
// Invariant: after consumed == maxAssets, any take with units > 0 on a buy offer
// with offerPrice < WAD must revert OR return (buyerAssets=0, sellerAssets=0, units=0).
function invariant_fullyConsumedBuyOfferNoStateChange() public {
    // Setup: offer.buy=true, maxAssets=N, consumed=N, tick < MAX_TICK
    // Call: take(units=1, ...)
    // Assert: reverts OR (creditOf unchanged AND debtOf unchanged AND totalUnits unchanged)
}
```

Expected assertion failures before fix:
- `assertEq(midnight.creditOf(id, lender), lenderCreditBefore)` — fails (credit increased by 1)
- `assertEq(midnight.debtOf(id, borrower), borrowerDebtBefore)` — fails (debt increased by 1)
- `assertEq(midnight.totalUnits(id), totalUnitsBefore)` — fails (totalUnits increased by 1)

### Citations

**File:** src/Midnight.sol (L363-363)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
```

**File:** src/Midnight.sol (L367-369)
```text
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L382-384)
```text
        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
```

**File:** src/Midnight.sol (L408-417)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);

        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
```

**File:** src/Midnight.sol (L445-453)
```text
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

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** certora/specs/Consume.spec (L88-97)
```text
/// If consumed is already at or above maxAssets before a take in assets mode, it remains unchanged.
rule takeConsumedAtMaxUnchangedAssets(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxUnits == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert consumedBefore >= offer.maxAssets => consumed(offer.maker, offer.group) == consumedBefore;
}
```

**File:** certora/specs/Consume.spec (L99-111)
```text
/// A fully-consumed offer in units mode only allows no-op takes.
rule fullyConsumedOfferRevertsOnNonTrivialTake(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxAssets == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    require offer.maxUnits > 0 && consumedBefore >= offer.maxUnits, "assume the offer is fully consumed";

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    // If take does not revert, its input has to be zero.
    assert units == 0;
}
```

**File:** test/TakeTest.sol (L857-889)
```text
    // Show that a buy offer with offerPrice < WAD can be taken with units > 0
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
