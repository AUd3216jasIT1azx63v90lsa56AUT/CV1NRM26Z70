### Title
Buy-offer asset-cap bypass via zero-rounding on low-price take — (`src/Midnight.sol`)

### Summary
When `offer.buy = true` and `offer.maxAssets > 0`, the consumed accounting tracks `buyerAssets = units.mulDivDown(buyerPrice, WAD)`. If `buyerPrice < WAD`, a take with `units = 1` produces `buyerAssets = 0`, so `consumed` does not increase and the cap check `newConsumed <= maxAssets` passes even when the offer is already fully consumed. The take still executes the full position state update, crediting the maker with extra units and increasing the taker's debt, with zero asset transfer.

### Finding Description
**Code path — `src/Midnight.sol` lines 363–369:**

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // rounds DOWN
    : units.mulDivUp(buyerPrice, WAD);

uint256 newConsumed;
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());   // only checks asset delta
}
```

**Root cause:** The cap guard adds `buyerAssets` (not `units`) to `consumed`. When `buyerPrice < WAD` (i.e., `offerPrice < WAD`, reachable at any tick below the WAD-equivalent tick, e.g. `MAX_TICK - 16` in the test suite), `mulDivDown(1, buyerPrice, WAD) = 0`. The guard therefore adds 0 and the check `M + 0 <= M` trivially passes.

**Exploit flow:**

1. Maker creates a buy offer with `maxAssets = M`, `tick` chosen so `offerPrice < WAD` (e.g. `MAX_TICK - 16`).
2. Taker (or anyone) fills the offer with units chosen so `units.mulDivDown(buyerPrice, WAD) = M` exactly → `consumed = M`.
3. Taker calls `take` again with `units = 1`. `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`. `newConsumed = M + 0 = M`. Check `M <= M` passes.
4. The function continues: `buyerCreditIncrease`, `sellerDebtIncrease`, and `totalUnits` are all updated for 1 unit. No assets are transferred (`buyerAssets - sellerAssets = 0`).
5. Step 3–4 can be repeated indefinitely.

**Why existing checks fail:** The `ConsumedAssets` guard at line 369 only bounds the running asset sum; it does not bound the number of units credited. When `buyerAssets = 0`, the guard is a no-op while the position accounting at lines 382–417 still executes in full.

The protocol's own NatSpec at line 94 acknowledges this: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."*

### Impact Explanation
After the asset cap `maxAssets` is fully consumed, an unprivileged taker can repeatedly call `take(units=1, ...)` on the same buy offer. Each call grants the maker one additional credit unit and increases the taker's debt by one unit, with zero loan-token transfer. The maker's credit grows without bound beyond the cap the maker intended, violating the invariant that a fully-consumed offer cannot be further filled.

### Likelihood Explanation
Preconditions: (a) `offer.buy = true`, (b) `offer.maxAssets > 0`, (c) `offerPrice < WAD` — satisfied by any tick below the WAD-equivalent tick, which is a normal, reachable part of the tick range. No privileged access, no oracle manipulation, and no special token behavior is required. The taker is fully unprivileged. The attack is repeatable in a single transaction via `multicall`.

### Recommendation
In the `maxAssets` branch, additionally require that `units == 0` whenever `buyerAssets == 0` (for buy offers) or `sellerAssets == 0` (for sell offers):

```solidity
if (offer.maxAssets > 0) {
    uint256 assetDelta = offer.buy ? buyerAssets : sellerAssets;
    require(assetDelta > 0 || units == 0, ZeroAssetNonZeroUnits());
    newConsumed = consumed[offer.maker][offer.group] += assetDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

This preserves the intended no-op take (units = 0) for callback purposes while closing the zero-rounding bypass.

### Proof of Concept
The existing Foundry test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` at lines 857–889 is a complete, passing proof of concept. It:

1. Sets `lenderOffer.maxAssets = 1`, `lenderOffer.tick = MAX_TICK - 16` (so `offerPrice < WAD`).
2. Pre-consumes the offer to its cap via `setConsumed(group, maxAssets, lender)`.
3. Calls `take(1, borrower, lenderOffer)` — i.e., `units = 1` on a fully-consumed offer.
4. Asserts `buyerAssets == 0` and `sellerAssets == 0` (no assets moved).
5. Asserts `creditOf(id, lender) > lenderCreditBefore` — maker gained credit beyond cap.
6. Asserts `debtOf(id, borrower) > borrowerDebtBefore` — taker gained debt.
7. Asserts `totalUnits(id) > totalUnitsBefore`.

All assertions pass, confirming the overfill. [1](#0-0) [2](#0-1) [3](#0-2)

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
