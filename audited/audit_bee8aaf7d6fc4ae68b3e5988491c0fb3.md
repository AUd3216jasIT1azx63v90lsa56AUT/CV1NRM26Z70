Audit Report

## Title
maxAssets Cap Bypass via Zero-Rounding `buyerAssets` on Buy Offers with `buyerPrice < WAD` - (File: src/Midnight.sol)

## Summary
When a buy offer has `maxAssets > 0` and `buyerPrice < WAD`, calling `take` with `units=1` causes `mulDivDown(1, buyerPrice, WAD)` to round `buyerAssets` to zero. The `consumed` accumulator is incremented by zero, so the cap check `newConsumed <= maxAssets` always passes regardless of how many times the fill is repeated. Position state (`credit`, `debt`, `totalUnits`) is mutated on every call while no loan tokens are transferred, violating the invariant that every credit unit corresponds to a real token transfer. The codebase explicitly documents this as a bug in `testBugBuyMaxAssetsBypass`.

## Finding Description
**Root cause** — `src/Midnight.sol` line 363:
```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // rounds DOWN to 0 when units*buyerPrice < WAD
    : units.mulDivUp(buyerPrice, WAD);
``` [1](#0-0) 

When `units * buyerPrice < WAD` (e.g., `units=1` and any `buyerPrice < WAD`), `mulDivDown` returns `0`. The cap enforcement block at lines 367–369 then increments `consumed` by `0` and checks `newConsumed <= maxAssets`, which trivially passes since `newConsumed` equals the pre-call value: [2](#0-1) 

Despite `buyerAssets = 0`, position state is unconditionally mutated at lines 408–417: `buyerPos.credit` increases, `sellerPos.debt` increases, and `totalUnits` increases. The token transfer at line 455 sends `buyerAssets - sellerAssets = 0` tokens. [3](#0-2) [4](#0-3) 

**Exploit flow:**
1. Attacker identifies a buy offer with `maxAssets > 0` and `tick < MAX_TICK` (so `buyerPrice < WAD`).
2. Optionally waits for or forces `consumed` to reach `maxAssets` (e.g., via `setConsumed`).
3. Repeatedly calls `take(offer, ratifierData, 1, taker, ...)` with `units=1`.
4. Each call: `buyerAssets=0`, `consumed` unchanged, cap check passes, but `lender.credit += 1`, `borrower.debt += 1`, `totalUnits += 1`.

**Why existing checks fail** — The only guard is `require(newConsumed <= offer.maxAssets)`. Since the increment is `0`, `newConsumed` never exceeds `maxAssets`. There is no guard of the form `require(buyerAssets > 0)` or `require(units == 0 || buyerAssets > 0)`. The ratifier (`SetterRatifier`, `EcrecoverRatifier`) validates the offer struct, not the fill amount, and passes on every call.

## Impact Explanation
The `maxAssets` cap — the maker's primary mechanism to bound total loan-token exposure — is rendered ineffective. An unprivileged taker can:
- Inflate the maker's `credit` position arbitrarily beyond `maxAssets` without the maker paying any loan tokens.
- Inflate their own `debt` position without receiving any tokens, corrupting the debt/asset accounting invariant.
- Inflate `totalUnits`, affecting fee accrual (`buyerPendingFeeIncrease` is computed per fill at line 385–386) and any downstream health checks that depend on `totalUnits`. [5](#0-4) 

This constitutes unauthorized state mutation and accounting integrity failure — both in-scope impact classes per RESEARCHER.md.

## Likelihood Explanation
**Required conditions:**
- `offer.buy = true` — standard lender offer.
- `offer.maxAssets > 0` — the cap feature being used.
- `buyerPrice < WAD` — satisfied for any tick below `MAX_TICK`; `tickToPrice` is ≤ WAD for all valid ticks, and the settlement fee subtraction makes this routine.
- `units=1` — always satisfies `1 * buyerPrice < WAD` when `buyerPrice < WAD`.

All conditions are met in normal protocol usage. The attack is permissionless, requires no special role, and is repeatable indefinitely within a single transaction via `multicall`. The existing test `testBugBuyMaxAssetsBypass` (lines 857–889) reproduces it exactly with `maxAssets=1` and `tick = MAX_TICK - 16`. [6](#0-5) 

## Recommendation
Add a guard immediately after computing `buyerAssets` and `sellerAssets` to reject fills that round to zero assets when `units > 0`:

```solidity
require(units == 0 || (offer.buy ? buyerAssets > 0 : sellerAssets > 0), ZeroAssetFill());
```

Alternatively, enforce a minimum `units` value such that `units * price >= WAD`, or use `mulDivUp` for the `consumed` accumulator on buy offers so that even a zero-asset fill still increments `consumed` by at least 1, ensuring the cap is eventually hit.

## Proof of Concept
The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` is a complete, passing reproduction: [7](#0-6) 

Key assertions confirming the bug:
- `assertEq(buyerAssets, 0)` — zero tokens transferred.
- `assertEq(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets)` — cap counter unchanged.
- `assertGt(midnight.creditOf(id, lender), lenderCreditBefore)` — maker credit inflated.
- `assertGt(midnight.debtOf(id, borrower), borrowerDebtBefore)` — taker debt inflated.
- `assertGt(midnight.totalUnits(id), totalUnitsBefore)` — global unit count inflated.

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

**File:** src/Midnight.sol (L385-386)
```text
        uint128 buyerPendingFeeIncrease =
            UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
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
