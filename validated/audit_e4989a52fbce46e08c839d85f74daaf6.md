Audit Report

## Title
Buy offer `maxAssets` cap bypassed via zero-rounding `buyerAssets` on sub-WAD price - (File: `src/Midnight.sol`)

## Summary
When a buy offer has `maxAssets > 0` and `tick < MAX_TICK` (i.e., `buyerPrice < WAD`), calling `take` with `units = 1` causes `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`. The consumed accumulator increments by zero, so the cap check `newConsumed <= maxAssets` is a no-op even when the offer is fully consumed. An unprivileged taker can call `take` indefinitely, minting unbacked lender credit and borrower debt with zero token transfer each call.

## Finding Description
**Exact code path** — `src/Midnight.sol` lines 363–369:

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // truncates to 0 when units * buyerPrice < WAD
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy
        ? buyerAssets   // += 0 → consumed unchanged
        : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());  // trivially passes
}
``` [1](#0-0) 

**Root cause** — `mulDivDown` performs integer division. For any `tick < MAX_TICK`, `buyerPrice < WAD`. With `units = 1`, `1 * buyerPrice / WAD = 0`. The consumed accumulator does not advance, and the cap check passes unconditionally regardless of the pre-existing consumed value.

**Position mutation still occurs** — Despite zero token flow, the position update logic at lines 382–417 executes normally: `buyerPos.credit += buyerCreditIncrease` (lender gains credit), `sellerPos.debt += sellerDebtIncrease` (borrower gains debt), and `_marketState.totalUnits` increases. The token transfer at line 455 sends `buyerAssets - sellerAssets = 0` tokens. [2](#0-1) [3](#0-2) 

**Why existing checks fail** — The sole guard is `require(newConsumed <= offer.maxAssets)`. When `buyerAssets = 0`, `newConsumed` equals the pre-call value, so the check never rejects. The NatSpec at line 94 explicitly acknowledges this: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* [4](#0-3) 

## Impact Explanation
The lender accumulates credit units without a corresponding loan token deposit. Credit represents a claim on loan tokens at maturity; credit created without deposit breaks the protocol's balance invariant. When the lender later redeems excess credit by taking a sell offer, the protocol pays out loan tokens sourced from other users' deposits — a direct theft of funds. Simultaneously, the borrower accumulates unbounded debt without receiving any assets, enabling manufacture of undercollateralized positions when the attacker controls both sides. `totalUnits` inflation also distorts any fee or settlement calculations that depend on it.

## Likelihood Explanation
Preconditions are the normal operating mode: any buy offer with `maxAssets > 0` and `tick < MAX_TICK` (every tick except the maximum) is vulnerable once consumed. The attacker can reach the consumed state themselves via legitimate fills. No privileged access, oracle manipulation, or token quirks are required. The attack is repeatable in a single transaction via `multicall` or a loop, and applies to every market. The only cost is gas and collateral to keep the borrower position healthy.

## Recommendation
Before incrementing the consumed accumulator, require that `buyerAssets > 0` when `offer.maxAssets > 0` and `offer.buy == true`:

```solidity
if (offer.maxAssets > 0) {
    uint256 delta = offer.buy ? buyerAssets : sellerAssets;
    require(delta > 0, ZeroAssets());   // prevent zero-rounding bypass
    newConsumed = consumed[offer.maker][offer.group] += delta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, enforce a minimum `units` such that `units * buyerPrice >= WAD` before proceeding, or switch to `mulDivUp` for the buy-side consumed delta (consistent with the sell-side rounding already used at line 363). [1](#0-0) 

## Proof of Concept
The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` lines 857–889 is a complete, passing reproduction: [5](#0-4) 

1. Set `lenderOffer.maxAssets = 1`, `lenderOffer.tick = MAX_TICK - 16` (so `buyerPrice < WAD`).
2. Call `setConsumed(group, maxAssets, lender)` to fully consume the offer.
3. Call `take(1, borrower, lenderOffer)` — returns `(buyerAssets=0, sellerAssets=0)`.
4. Assert: `consumed` unchanged at `maxAssets`; token balances unchanged; but `creditOf(lender)` increased, `debtOf(borrower)` increased, `totalUnits` increased.
5. Repeat step 3 indefinitely — each iteration advances lender credit and borrower debt by 1 unit at zero token cost.

### Citations

**File:** src/Midnight.sol (L94-94)
```text
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

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
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
