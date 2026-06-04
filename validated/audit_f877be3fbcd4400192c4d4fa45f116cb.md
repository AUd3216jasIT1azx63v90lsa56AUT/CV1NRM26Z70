### Title
`maxAssets` Cap on Buy Offers Bypassed via Zero-Rounding When `offerPrice < WAD` — (File: `src/Midnight.sol`)

---

### Summary

In `Midnight.sol`'s `take()` function, the consumed-cap enforcement for assets-based buy offers (`offer.buy = true`, `offer.maxAssets > 0`) increments the consumed counter by `buyerAssets`, which is computed with `mulDivDown`. When `offerPrice < WAD` and `units` is small enough, `buyerAssets` rounds down to zero. The consumed counter therefore never advances past `maxAssets`, and the cap check `require(newConsumed <= offer.maxAssets)` passes indefinitely — even after the offer is fully consumed. This is the direct analog of the external report's "zero means infinite" sentinel bypass: instead of a supply counter reaching zero and being misread as "no cap," here the per-take asset increment rounds to zero and the cap is never re-triggered.

---

### Finding Description

**Root cause — `src/Midnight.sol` lines 367–373:**

```solidity
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
} else { ... }
``` [1](#0-0) 

`buyerAssets` is computed two lines earlier as:

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
``` [2](#0-1) 

`mulDivDown` is integer division that truncates:

```solidity
function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y) / d;
}
``` [3](#0-2) 

When `buyerPrice < WAD` (i.e., `offerPrice < WAD`, reachable at any tick below the WAD threshold) and `units` is small enough that `units * buyerPrice < WAD`, `buyerAssets = 0`. The consumed counter is incremented by 0, so `newConsumed` stays at `maxAssets`, and `require(newConsumed <= maxAssets)` passes. The take succeeds with non-zero `units`, increasing the lender's credit and the borrower's debt, while transferring zero tokens.

The protocol itself acknowledges this at line 94:

> `@dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.` [4](#0-3) 

The test suite even names the demonstration `testBugBuyMaxAssetsBypass` and confirms the exact state change: [5](#0-4) 

---

### Impact Explanation

A taker can repeatedly call `take()` against a fully consumed buy offer (with `offerPrice < WAD`) using small `units` values. Each successful call:

- Increases the **lender's credit** beyond the `maxAssets` cap they set to bound their exposure.
- Increases the **borrower's debt** by the same amount, with zero tokens transferred in either direction.
- Leaves `consumed[maker][group]` unchanged (still equal to `maxAssets`), so the cap is never re-enforced.

The lender's `maxAssets` parameter is documented as capping "max buyer assets" — i.e., the total loan tokens the lender commits. By bypassing it, the lender accumulates credit units beyond their intended risk limit. If the market later realizes bad debt, the lender's loss is proportional to their credit, so they lose more than they intended to risk. The bypass is unbounded and repeatable as long as the borrower can post sufficient collateral to satisfy the health check.

---

### Likelihood Explanation

**Preconditions (all reachable without privilege):**

1. A buy offer exists with `offerPrice < WAD` — any tick below the WAD threshold qualifies; the test uses `MAX_TICK - 16`.
2. `offer.maxAssets > 0` — the maker set an asset cap (the common case for risk-managed offers).
3. The offer is fully consumed — the attacker can do this themselves by taking up to `maxAssets` legitimately, or the offer may already be at cap.
4. The attacker calls `take(units=1, ...)` — a single unit suffices when `buyerPrice < WAD`.
5. The attacker must hold enough collateral to remain healthy after each debt increment.

No admin keys, no leaked secrets, no oracle manipulation required. Any taker interacting with a low-tick buy offer can trigger this.

---

### Recommendation

Track consumed in **units** rather than assets for the cap check, or enforce a minimum `buyerAssets > 0` when `units > 0`. Concretely, add a guard before the consumed update:

```solidity
if (offer.maxAssets > 0) {
    uint256 assetsDelta = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || assetsDelta > 0, ZeroAssetNonZeroUnits());
    newConsumed = consumed[offer.maker][offer.group] += assetsDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, switch assets-capped offers to track consumed in units (as the `maxUnits` path already does), and derive the asset cap check from the cumulative units × price. This mirrors the fix suggested in the external report: use a sentinel that cannot be reached through normal operations (`type(uint256).max` instead of `0`), or here, ensure the increment is always positive when `units > 0`.

---

### Proof of Concept

The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` is a complete, passing reproduction: [5](#0-4) 

**Step-by-step:**

1. Lender creates a buy offer: `maxAssets = 1`, `tick = MAX_TICK - 16` (so `offerPrice < WAD`).
2. Lender calls `setConsumed(group, 1, lender)` — offer is now fully consumed.
3. Borrower supplies collateral and calls `take(units=1, borrower, lenderOffer)`.
4. Inside `take`: `buyerAssets = 1 * buyerPrice / WAD = 0` (truncated). `newConsumed = 1 + 0 = 1`. `require(1 <= 1)` passes.
5. Result: `creditOf(lender)` increases, `debtOf(borrower)` increases, `consumed` stays at 1, zero tokens move.
6. Step 3–5 can be repeated indefinitely, growing the lender's credit without bound.

### Citations

**File:** src/Midnight.sol (L94-94)
```text
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L363-364)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
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

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
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
