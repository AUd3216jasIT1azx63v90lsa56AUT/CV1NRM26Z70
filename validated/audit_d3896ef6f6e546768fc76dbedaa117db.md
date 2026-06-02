The code, test, and protocol comments all confirm this claim. Here is the validated finding:

---

Audit Report

## Title
Fully-consumed assets-based buy offer with `buyerPrice < WAD` can be taken indefinitely with zero asset transfer but non-zero position mutations - (File: src/Midnight.sol)

## Summary
When a buy offer with `maxAssets > 0` is fully consumed and `buyerPrice < WAD`, calling `take()` with `units = 1` causes `mulDivDown(1, buyerPrice, WAD)` to truncate to `0`. The consumed counter does not advance, so the `ConsumedAssets` guard passes indefinitely, while all position mutations (`credit`, `debt`, `totalUnits`) execute on the full `units = 1` value with zero token transfer. The protocol explicitly acknowledges this behavior at line 94 and the test `testBugBuyMaxAssetsBypass` reproduces it exactly.

## Finding Description
**Code path — `src/Midnight.sol` lines 363–373:**

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // = 0 when buyerPrice < WAD, units = 1
    : units.mulDivUp(buyerPrice, WAD);

uint256 newConsumed;
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets()); // maxAssets + 0 ≤ maxAssets → passes
}
``` [1](#0-0) 

When `buyerPrice < WAD` (e.g., `tick = MAX_TICK - 16`) and `units = 1`, `buyerAssets = 0`. Adding `0` to an already-maxed `consumed` value satisfies `<= maxAssets`, so the guard passes. Position mutations at lines 408–417 then execute unconditionally on `units = 1`: [2](#0-1) 

**Protocol acknowledgement at line 94:** [3](#0-2) 

**Why existing checks fail:**
- `require(newConsumed <= offer.maxAssets)` — passes because `maxAssets + 0 ≤ maxAssets`
- No check enforces `units == 0` when `consumed >= maxAssets`
- No check enforces `buyerAssets > 0` when `units > 0`

## Impact Explanation
After an offer reaches `consumed == maxAssets`, any unprivileged taker can call `take(offer, ..., units=1)` indefinitely. Each call increases the buyer/maker's `credit`, the seller/taker's `debt`, and `totalUnits` by amounts derived from `units = 1`, while transferring zero loan tokens. This corrupts the credit/debt accounting invariant and inflates `totalUnits` without any corresponding asset backing, at zero token cost to the attacker. The test `testBugBuyMaxAssetsBypass` explicitly asserts `buyerAssets == 0`, `sellerAssets == 0`, token balances unchanged, yet `creditOf`, `debtOf`, and `totalUnits` all strictly increase. [4](#0-3) 

## Likelihood Explanation
All preconditions are attacker-reachable without any privileged access:
1. `offer.buy = true` and `offer.maxAssets > 0` — standard offer configuration
2. `buyerPrice < WAD` — achievable at any tick where `tickToPrice(tick) + settlementFee < WAD` (e.g., `MAX_TICK - 16`)
3. Offer fully consumed — attacker can self-consume as first taker, or wait for organic fills

The attack is repeatable in every block at negligible gas cost since no token transfers occur.

## Recommendation
Add a guard immediately after computing `buyerAssets` to reject takes where `units > 0` but `buyerAssets == 0` for buy offers with `maxAssets > 0`. Alternatively, enforce that `consumed + units > consumed` (i.e., the consumed counter must strictly advance) before allowing position mutations. A minimal fix:

```solidity
if (offer.maxAssets > 0 && offer.buy) {
    require(units == 0 || buyerAssets > 0, ZeroAssetTake());
}
```

This prevents the truncation bypass while preserving zero-unit no-op takes (which the protocol explicitly allows per line 93). [5](#0-4) 

## Proof of Concept
The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` (lines 858–889) is a complete, self-contained reproduction:

1. Set `lenderOffer.maxAssets = 1`, `lenderOffer.tick = MAX_TICK - 16` (price < WAD)
2. Manually set `consumed` to `maxAssets` via `midnight.setConsumed()`
3. Call `take(1, borrower, lenderOffer)`
4. Observe: `buyerAssets == 0`, `sellerAssets == 0`, token balances unchanged
5. Observe: `creditOf(id, lender)` increased, `debtOf(id, borrower)` increased, `totalUnits` increased [4](#0-3)

### Citations

**File:** src/Midnight.sol (L93-94)
```text
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L363-373)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
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
