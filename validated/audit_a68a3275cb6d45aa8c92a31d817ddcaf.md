Audit Report

## Title
Buy offer `maxAssets` cap bypassed via dust rounding when `buyerPrice < WAD` - (File: `src/Midnight.sol`)

## Summary
When a buy offer uses `maxAssets` as its fill cap and `buyerPrice < WAD` (i.e., `tick < MAX_TICK`), calling `take` with `units = 1` after the offer is fully consumed produces `buyerAssets = 0` due to `mulDivDown` truncation. The `ConsumedAssets` guard then passes because `newConsumed = maxAssets + 0 <= maxAssets`, yet the rest of `take` still processes `units = 1`, minting real credit and debt with zero asset transfer. This is repeatable indefinitely by any unprivileged taker.

## Finding Description
In `src/Midnight.sol`, `take` computes asset amounts before enforcing the cap:

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
``` [1](#0-0) 

The cap check then increments `consumed` by `buyerAssets` (for buy offers):

```solidity
newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
require(newConsumed <= offer.maxAssets, ConsumedAssets());
``` [2](#0-1) 

When `buyerPrice < WAD` and `units = 1`, `mulDivDown(1, buyerPrice, WAD) = 0`. If `consumed[maker][group]` already equals `offer.maxAssets`, then `newConsumed = maxAssets + 0 = maxAssets`, satisfying `<= maxAssets`. The guard passes, but execution continues to the position mutation block:

```solidity
buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);
sellerPos.debt   += UtilsLib.toUint128(sellerDebtIncrease);
_marketState.totalUnits = UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
``` [3](#0-2) 

Each such call increases `buyerPos.credit`, `sellerPos.debt`, and `_marketState.totalUnits` by 1 unit with zero token transfer (`buyerAssets = sellerAssets = 0`).

**Exploit flow:**
1. Maker creates a buy offer with `maxAssets = N` and `tick < MAX_TICK` (common case for any below-par offer).
2. Taker fills the offer normally until `consumed[maker][group] == N`.
3. Taker calls `take(offer, ..., units=1, ...)` repeatedly.
4. Each call: `buyerAssets = 0`, cap check passes, lender gains +1 credit, borrower gains +1 debt, `totalUnits` +1, no tokens move.
5. Step 4 is unbounded.

The test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` explicitly documents and confirms this behavior, asserting that `creditOf(lender)`, `debtOf(borrower)`, and `totalUnits` all increase while `consumed` stays at `maxAssets` and both asset amounts are zero: [4](#0-3) 

## Impact Explanation
The `maxAssets` cap on buy offers is completely ineffective for dust fills when `buyerPrice < WAD`. An unprivileged taker can force the maker (lender) to accumulate unbounded credit beyond their stated limit, and simultaneously accumulate unbounded debt for themselves without paying any assets. This violates the core protocol invariant that every unit of credit corresponds to a real asset deposit, and that offers cannot be overfilled. `totalUnits` grows without bound, corrupting the market's accounting state.

## Likelihood Explanation
The precondition `buyerPrice < WAD` holds whenever `tick < MAX_TICK`, which is the normal case for any offer priced below par. The attacker requires no privileges: they only need to (a) observe the offer parameters on-chain, (b) fill the offer to `maxAssets` through normal takes, and (c) call `take` with `units = 1`. The attack is automatable in a single transaction via `multicall` or a loop contract. [5](#0-4) 

## Recommendation
Add a guard that rejects a `take` call when `units > 0` but the computed asset amount rounds to zero. For buy offers with `maxAssets > 0`, require `buyerAssets > 0` whenever `units > 0`:

```solidity
if (offer.maxAssets > 0 && offer.buy) {
    require(units == 0 || buyerAssets > 0, ZeroDustTake());
}
```

Alternatively, track consumed in `units` rather than `buyerAssets` when `buyerAssets` would round to zero, or enforce `require(units == 0 || buyerAssets > 0 || sellerAssets > 0)` unconditionally to prevent any zero-asset fill with nonzero units.

## Proof of Concept
The existing test `testBugBuyMaxAssetsBypass` at line 858 of `test/TakeTest.sol` is a complete, self-contained reproduction: [6](#0-5) 

Run with: `forge test --match-test testBugBuyMaxAssetsBypass -vvv`

The test passes (i.e., the bug is confirmed) when all three `assertGt` assertions hold: `creditOf(lender) > lenderCreditBefore`, `debtOf(borrower) > borrowerDebtBefore`, and `totalUnits(id) > totalUnitsBefore`, despite `consumed` remaining at `maxAssets` and both `buyerAssets` and `sellerAssets` being zero.

### Citations

**File:** src/Midnight.sol (L363-369)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L410-417)
```text
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
