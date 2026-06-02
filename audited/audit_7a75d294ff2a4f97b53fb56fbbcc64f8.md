Audit Report

## Title
Assets-based buy offer `maxAssets` cap bypassed via rounding-to-zero when `buyerPrice < WAD` - (`src/Midnight.sol`)

## Summary
When a buy offer has `maxAssets > 0` and `buyerPrice < WAD`, calling `take` with a small `units` value causes `mulDivDown(units, buyerPrice, WAD)` to round to zero. The consumed tracking increments by zero, so the `require(newConsumed <= offer.maxAssets)` guard passes even when the offer is already fully exhausted. The protocol's own test suite (`testBugBuyMaxAssetsBypass`) and inline documentation (line 94) confirm this is a known, reproducible accounting failure.

## Finding Description

**Root cause** — `src/Midnight.sol`, `take()`: [1](#0-0) 

Line 363 computes `buyerAssets = units.mulDivDown(buyerPrice, WAD)`. When `buyerPrice < WAD` (e.g. tick `MAX_TICK - 16`) and `units = 1`, this evaluates to `0`. Line 368 then does `consumed[maker][group] += 0`, leaving `consumed` unchanged at `maxAssets`. Line 369's `require(newConsumed <= offer.maxAssets)` becomes `require(maxAssets <= maxAssets)` and passes unconditionally.

The protocol explicitly acknowledges this edge case in its own NatDev comments: [2](#0-1) 

**Exploit flow:**
1. Maker creates a buy offer: `maxAssets = 1`, `tick = MAX_TICK - 16` (so `buyerPrice < WAD`).
2. Offer is fully consumed: `consumed[maker][group] == 1 == maxAssets` (via prior fills or `setConsumed`).
3. Taker calls `take(units=1, taker=borrower, offer=lenderOffer)`.
4. `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`; `consumed += 0`; guard passes.
5. Execution continues: `buyerPos.credit += 1`, `sellerPos.debt += 1`, `totalUnits += 1`.
6. No tokens are transferred (`buyerAssets = sellerAssets = 0`).
7. Steps 3–6 repeat indefinitely.

**Why existing checks fail:** The sole guard is `require(newConsumed <= offer.maxAssets)`. There is no independent check that `units == 0` or that `buyerAssets > 0` before mutating positions. The rounding behavior structurally guarantees bypass whenever `buyerPrice < WAD` and `units` is sufficiently small.

## Impact Explanation

Each bypass call grants the maker (lender) credit units without the maker paying any loan-token assets, and forces the taker (borrower) to absorb debt without receiving any assets. `totalUnits` is inflated without backing collateral, corrupting the credit/debt accounting invariant that the protocol's solvency depends on. The `maxAssets` cap — the primary mechanism for bounding offer exposure — is rendered ineffective for this class of offers. [3](#0-2) 

## Likelihood Explanation

All preconditions are reachable by any unprivileged user: `offer.buy = true` is a standard offer type; `maxAssets > 0` is a common cap pattern; ticks below the WAD threshold are explicitly supported by the protocol; and `consumed == maxAssets` is the normal post-fill state. The attack requires no oracle manipulation, admin access, or token misbehavior. It is repeatable on every block with `units = 1`. The authorization check at line 346 (`require(taker == msg.sender || isAuthorized[taker][msg.sender]`) means the taker must be the caller or must have authorized the caller — preventing forced debt on arbitrary third parties, but not preventing a willing or deceived taker from triggering the path. [4](#0-3) 

## Recommendation

Add an explicit guard that rejects a take when the offer is assets-capped and the computed asset increment rounds to zero while the offer is already fully consumed:

```solidity
// After computing buyerAssets / sellerAssets, before the consumed check:
if (offer.maxAssets > 0 && (offer.buy ? buyerAssets : sellerAssets) == 0) {
    require(consumed[offer.maker][offer.group] < offer.maxAssets, ConsumedAssets());
}
```

Alternatively, require `units > 0` AND that the resulting asset delta is nonzero before allowing position mutation, or use `mulDivUp` for the consumed increment on buy offers to ensure rounding always advances the counter.

## Proof of Concept

The protocol's own test suite provides a complete, passing reproduction: [5](#0-4) 

`testBugBuyMaxAssetsBypass` (lines 857–889 of `test/TakeTest.sol`):
- Sets `maxAssets = 1`, `tick = MAX_TICK - 16`
- Pre-sets `consumed` to `maxAssets` via `setConsumed`
- Calls `take(1, borrower, lenderOffer)` on the exhausted offer
- Asserts `buyerAssets == 0`, `sellerAssets == 0`, token balances unchanged, `consumed` unchanged — yet `creditOf(lender)`, `debtOf(borrower)`, and `totalUnits` all strictly increased

### Citations

**File:** src/Midnight.sol (L94-94)
```text
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L346-346)
```text
        require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
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

**File:** src/Midnight.sol (L416-418)
```text
        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
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
