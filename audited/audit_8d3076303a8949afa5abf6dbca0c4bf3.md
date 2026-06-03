Audit Report

## Title
Buy-offer `maxAssets` cap bypassed via rounding-to-zero `buyerAssets` on small-unit takes - (File: src/Midnight.sol)

## Summary
When a buy offer has `maxAssets > 0` and `buyerPrice < WAD`, the `consumed` counter is incremented by `buyerAssets = units.mulDivDown(buyerPrice, WAD)`, which truncates to zero for sufficiently small `units`. Because `consumed` never increases, the `newConsumed <= offer.maxAssets` guard passes unconditionally, yet real credit, debt, and `totalUnits` are still created. An unprivileged attacker can overfill the maker's offer in unit terms indefinitely while the asset-denominated cap is never breached in accounting.

## Finding Description
**Code path — `src/Midnight.sol` lines 363–369:**

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // rounds DOWN to zero
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

**Root cause:** For a buy offer, `consumed` is incremented by `buyerAssets`. When `buyerPrice < WAD` and `units * buyerPrice < WAD`, `mulDivDown` returns 0. `consumed` is unchanged, so `newConsumed <= offer.maxAssets` is trivially satisfied regardless of how many times `take()` is called.

**Exploit flow:**
1. Maker creates a buy offer with `maxAssets = N` and `tick > MAX_TICK/2` (so `buyerPrice < WAD`).
2. Attacker calls `take(units=1, ...)` repeatedly (or in a single transaction via callback).
3. Each call: `buyerAssets = 0`, `consumed` unchanged, cap check passes, but `creditOf`, `debtOf`, and `totalUnits` all increase by 1 unit (lines 408–417).
4. This continues indefinitely — even after `consumed` has already reached `maxAssets` via normal fills.

**Why existing checks fail:** The sole guard is `require(newConsumed <= offer.maxAssets)`. Since `newConsumed` never increases when `buyerAssets = 0`, this check is permanently satisfied. No other check bounds the number of unit-level fills.

**Protocol acknowledgment:** The NatSpec at line 94 states: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* The test `testBugBuyMaxAssetsBypass` (lines 857–889 of `test/TakeTest.sol`) explicitly confirms: after pre-consuming the offer to `maxAssets`, a `take(1, ...)` succeeds, `consumed` stays at `maxAssets`, but `creditOf`, `debtOf`, and `totalUnits` all strictly increase.

## Impact Explanation
The maker's `maxAssets` cap is intended to bound total economic exposure in asset terms. Due to rounding, an attacker can create unbounded credit and debt on behalf of the maker beyond what `maxAssets / buyerPrice` would imply. The maker's offer is overfilled in unit terms while the asset-denominated `consumed` counter never exceeds `maxAssets`, so no revert ever occurs. This constitutes unauthorized state change — unbounded debt/credit creation — against the maker's expressed intent, violating the core invariant that offers cannot be overfilled beyond their cap.

## Likelihood Explanation
Preconditions are easily satisfied: any buy offer at a tick above `MAX_TICK/2` (i.e., `buyerPrice < WAD`) with `maxAssets > 0` is vulnerable. The attacker requires no special privilege — any address can call `take()` as the taker. The attack is repeatable in a single transaction via callback or across multiple transactions. Ticks above `MAX_TICK/2` are common for lenders offering below-par rates, making this a realistic and broadly applicable condition.

## Recommendation
Replace the `buyerAssets`-based increment with a unit-denominated floor when `buyerAssets` rounds to zero. Concretely, when `offer.maxAssets > 0` and `offer.buy` is true, increment `consumed` by `max(buyerAssets, 1)` (or equivalently use `mulDivUp` for the consumed accounting even on buy offers) so that every non-zero unit take advances the cap counter by at least 1. Alternatively, add a secondary `maxUnits` guard that is enforced in parallel with `maxAssets` to bound unit-level overfill independently of asset rounding.

## Proof of Concept
The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` (lines 857–889) is a self-contained, passing reproduction:

1. Set `lenderOffer.maxAssets = 1`, `lenderOffer.tick = MAX_TICK - 16` (so `buyerPrice < WAD`).
2. Pre-consume the offer to `maxAssets` via `midnight.setConsumed(...)`.
3. Call `take(1, borrower, lenderOffer)`.
4. Assert: `consumed` is unchanged at `maxAssets`, token balances are unchanged, yet `creditOf(lender)`, `debtOf(borrower)`, and `totalUnits` all strictly increase.

The test passes as written, confirming the bypass is real and reproducible. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** src/Midnight.sol (L93-94)
```text
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L363-369)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
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
