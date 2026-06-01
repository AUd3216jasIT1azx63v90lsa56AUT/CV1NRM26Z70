### Title
No-op take (units=0) on fully consumed maxAssets offer invokes maker callback indefinitely — (`src/Midnight.sol`)

### Summary

When `offer.maxAssets > 0` and `consumed[offer.maker][offer.group] == offer.maxAssets`, calling `take` with `units=0` adds zero to `consumed`, making the `require(newConsumed <= offer.maxAssets)` check pass trivially. Because no guard prevents callback dispatch on a zero-unit take, the maker's `onBuy`/`onSell` callback is invoked with all-zero arguments. This can be repeated indefinitely by any unprivileged taker.

### Finding Description

**Exact code path — `src/Midnight.sol` `take()` function:**

With `units = 0`, `offer.maxAssets > 0`, `consumed[maker][group] == offer.maxAssets`, `offer.callback != address(0)`:

**Step 1 — asset computation (lines 363–364):**
```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
// units=0 → buyerAssets=0, sellerAssets=0
``` [1](#0-0) 

**Step 2 — consumed check (lines 367–369):**
```solidity
newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
// += 0 → newConsumed == offer.maxAssets
require(newConsumed <= offer.maxAssets, ConsumedAssets());
// require(maxAssets <= maxAssets) → PASSES every time
``` [2](#0-1) 

**Step 3 — callback dispatch (lines 420–421, 445–453, 458–474):**
```solidity
address buyerCallback = offer.buy ? offer.callback : takerCallback;
address sellerCallback = offer.buy ? takerCallback : offer.callback;
// ...
if (buyerCallback != address(0)) {
    IBuyCallback(buyerCallback).onBuy(id, market, 0, 0, 0, buyer, callbackData);
}
// ...
if (sellerCallback != address(0)) {
    ISellCallback(sellerCallback).onSell(id, market, 0, 0, 0, seller, receiver, callbackData);
}
``` [3](#0-2) [4](#0-3) [5](#0-4) 

There is no guard of the form `require(units > 0)` or `require(newConsumed > consumedBefore)` before callback dispatch. The token transfers at lines 455–456 transfer zero, which succeeds for standard ERC20 tokens. [6](#0-5) 

**Why existing protections fail:**
- `ConsumedAssets` only fires when `newConsumed > maxAssets`; adding 0 never triggers it.
- The Certora `fullyConsumedOfferRevertsOnNonTrivialTake` rule (lines 99–111 of `Consume.spec`) is scoped to `offer.maxAssets == 0` (units mode) only — the assets-mode equivalent is absent.
- `takeConsumedAtMaxUnchangedAssets` (lines 88–97) only asserts `consumed` doesn't change, not that callbacks are suppressed. [7](#0-6) [8](#0-7) 

The existing test `testBugBuyMaxAssetsBypass` (lines 858–889) already demonstrates the related zero-assets bypass with `units=1` at a low tick, confirming the consumed-check gap is known for the assets branch. The `units=0` path is a direct, price-independent variant. [9](#0-8) 

### Impact Explanation

Any unprivileged taker can invoke the maker's `onBuy` or `onSell` callback contract an unlimited number of times after the offer is fully consumed, passing all-zero amounts. If the callback performs state mutations (e.g., collateral deposits, approvals, counter increments, token pulls from an allowance) or emits events, those side effects are triggered without any real fill. This constitutes unbounded gas grief and potential state corruption of the maker's callback contract.

### Likelihood Explanation

Preconditions are easily reachable: any offer with `maxAssets > 0` that has been fully consumed (a normal lifecycle event) and has a non-zero `offer.callback` is permanently vulnerable. The attacker needs no special role — only the ability to call `take` as `msg.sender == taker`. The attack is repeatable in a single transaction via `multicall` or across multiple transactions, with no cost beyond gas.

### Recommendation

Add a guard before callback dispatch (or immediately after the consumed update) that skips the rest of the function body when no real fill occurred:

```solidity
// After the consumed block, before position updates:
if (units == 0) return (0, 0);
```

Alternatively, gate callback invocation explicitly:

```solidity
if (buyerCallback != address(0) && units > 0) { ... }
if (sellerCallback != address(0) && units > 0) { ... }
```

The Certora `Consume.spec` should also add a `fullyConsumedOfferRevertsOnNonTrivialTake` rule for the `maxAssets > 0` branch, mirroring the existing units-mode rule.

### Proof of Concept

```solidity
// Foundry unit test
function testNoOpTakeGriefsCallback() public {
    // Setup: buy offer with maxAssets=100, callback=address(countingCallback)
    CountingCallback cb = new CountingCallback(); // records invocation count
    lenderOffer.maxUnits = 0;
    lenderOffer.maxAssets = 100e18;
    lenderOffer.callback = address(cb);

    // Fully consume the offer legitimately
    vm.prank(lender);
    midnight.setConsumed(lenderOffer.group, lenderOffer.maxAssets, lender);
    assertEq(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets);

    // Attacker calls take with units=0 three times
    for (uint i = 0; i < 3; i++) {
        vm.prank(borrower);
        midnight.take(lenderOffer, hex"", 0, borrower, borrower, address(0), hex"");
    }

    // Assert: callback was invoked 3 times despite offer being fully consumed
    assertEq(cb.invocationCount(), 3); // FAILS if bug is fixed, PASSES if bug exists
    // consumed unchanged
    assertEq(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets);
}
```

Expected assertion: `cb.invocationCount() == 3` demonstrates the maker's callback is called 3 times after the offer is exhausted, with zero assets transferred each time.

### Citations

**File:** src/Midnight.sol (L363-364)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/Midnight.sol (L367-369)
```text
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L420-421)
```text
        address buyerCallback = offer.buy ? offer.callback : takerCallback;
        address sellerCallback = offer.buy ? takerCallback : offer.callback;
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

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/Midnight.sol (L458-474)
```text
        if (sellerCallback != address(0)) {
            bytes memory sellerCallbackData = offer.buy ? takerCallbackData : offer.callbackData;
            require(
                ISellCallback(sellerCallback)
                    .onSell(
                        id,
                        offer.market,
                        sellerAssets,
                        units,
                        sellerPendingFeeDecrease,
                        seller,
                        receiver,
                        sellerCallbackData
                    ) == CALLBACK_SUCCESS,
                WrongSellCallbackReturnValue()
            );
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
