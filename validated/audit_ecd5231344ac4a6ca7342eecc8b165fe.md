Audit Report

## Title
Fully-consumed assets-based buy offer with `buyerPrice < WAD` can be taken indefinitely with zero asset transfer but non-zero position state mutations - (File: `src/Midnight.sol`)

## Summary
When a buy offer with `maxAssets > 0` is fully consumed and `buyerPrice < WAD`, a taker can repeatedly call `take` with `units = 1`, producing `buyerAssets = mulDivDown(1 * buyerPrice, WAD) = 0`. Adding zero to an already-maxed `consumed` value satisfies the `<= maxAssets` guard, so all subsequent position mutations at lines 408–417 execute unconditionally on `units = 1`, inflating the maker's `credit`, the taker's `debt`, and `totalUnits` without any corresponding token transfer or consumed increment.

## Finding Description
**Root cause:** In `src/Midnight.sol` at lines 363–369, the consumed guard increments by `buyerAssets` (not `units`) for buy offers:

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // rounds DOWN
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

When `buyerPrice < WAD` (achievable at any tick where `tickToPrice(tick) + settlementFee < WAD`, e.g. `MAX_TICK - 16`), `mulDivDown(1 * buyerPrice, WAD)` truncates to `0`. If `consumed` already equals `maxAssets`, then `newConsumed = maxAssets + 0 = maxAssets ≤ maxAssets` passes the guard.

All position mutations at lines 408–417 then execute on `units = 1`:
- `buyerPos.credit += buyerCreditIncrease` (derived from `units`)
- `sellerPos.debt += sellerDebtIncrease` (derived from `units`)
- `_marketState.totalUnits += buyerCreditIncrease`

No check enforces `units == 0` when `consumed >= maxAssets`, and no check enforces `buyerAssets > 0` when `units > 0`.

**Exploit flow:**
1. Attacker creates a buy offer with `maxAssets > 0`, `maxUnits = 0`, and `tick` chosen so `buyerPrice < WAD`
2. Offer is fully consumed (attacker self-consumes or waits for organic fills)
3. Attacker calls `take(offer, ..., units=1)` repeatedly — each call passes the consumed guard, transfers zero tokens, but increments maker credit, taker debt, and `totalUnits` by amounts derived from `units = 1`

The protocol explicitly acknowledges this at `src/Midnight.sol` line 94: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."*

## Impact Explanation
After an offer reaches `consumed == maxAssets`, any unprivileged taker can call `take` indefinitely at negligible gas cost (zero token transfers occur). Each call inflates the maker's `credit`, the taker's `debt`, and `totalUnits` without any asset backing. This corrupts the core credit/debt accounting invariant and inflates `totalUnits` unboundedly, which affects all downstream logic that depends on these values (e.g. settlement, liquidation thresholds, fee calculations). This constitutes unauthorized state corruption and accounting integrity failure.

## Likelihood Explanation
All preconditions are attacker-reachable without any privileged access:
1. `offer.buy = true`, `maxAssets > 0` — standard offer configuration
2. `buyerPrice < WAD` — any tick below the WAD threshold (e.g. `MAX_TICK - 16`), no special access required
3. Offer fully consumed — attacker can self-consume as the first taker

The attack is repeatable every block with zero token cost. Any address that is not the maker can execute it.

## Recommendation
Add a guard that rejects a take when `units > 0` but `buyerAssets == 0` for buy offers with `maxAssets > 0`. Concretely, after computing `buyerAssets`, require:

```solidity
if (offer.maxAssets > 0 && offer.buy) {
    require(units == 0 || buyerAssets > 0, ZeroBuyerAssets());
}
```

Alternatively, enforce that `consumed >= maxAssets` implies `units == 0` before any state mutation, or switch the consumed accounting to track `units` instead of `buyerAssets` for buy offers.

## Proof of Concept
The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` at line 858 is a complete, passing reproduction:

```solidity
function testBugBuyMaxAssetsBypass() public {
    lenderOffer.maxUnits = 0;
    lenderOffer.maxAssets = 1;
    lenderOffer.tick = MAX_TICK - 16; // buyerPrice < WAD

    // Pre-consume the offer to maxAssets
    vm.prank(lender);
    midnight.setConsumed(lenderOffer.group, lenderOffer.maxAssets, lender);

    // Take with units=1 on a fully-consumed offer
    (uint256 buyerAssets, uint256 sellerAssets) = take(1, borrower, lenderOffer);

    assertEq(buyerAssets, 0);   // zero tokens transferred
    assertEq(sellerAssets, 0);
    assertEq(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets); // consumed unchanged

    // But position state mutated:
    assertGt(midnight.creditOf(id, lender), lenderCreditBefore);
    assertGt(midnight.debtOf(id, borrower), borrowerDebtBefore);
    assertGt(midnight.totalUnits(id), totalUnitsBefore);
}
```

The test passes, confirming the exploit path is live in the current codebase.