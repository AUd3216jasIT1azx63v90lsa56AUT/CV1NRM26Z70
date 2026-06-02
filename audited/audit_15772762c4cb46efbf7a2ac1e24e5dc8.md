Audit Report

## Title
Buy-offer `maxAssets` cap bypassed via `mulDivDown` rounding to zero in `take()` — (File: `src/Midnight.sol`)

## Summary
In `take()`, when processing a buy offer with `maxAssets > 0`, the consumed counter is incremented by `buyerAssets = units.mulDivDown(buyerPrice, WAD)`. When `units * buyerPrice < WAD`, this rounds to zero, so `consumed[maker][group]` never advances and the `require(newConsumed <= offer.maxAssets)` guard passes trivially. A taker can therefore fill the offer an unbounded number of times, accumulating real credit and debt positions in the protocol while the maker's cap is never enforced.

## Finding Description
**Exact code path** — `src/Midnight.sol` lines 363–369:

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // rounds DOWN → can be 0
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
``` [1](#0-0) 

**Root cause**: `mulDivDown(units, buyerPrice, WAD)` returns `0` whenever `units * buyerPrice < WAD`. Since `tickToPrice` returns values `<= WAD`, a taker can always choose `units` small enough (e.g., `units = 1` when `buyerPrice < WAD`) to produce `buyerAssets = 0`. The consumed counter then increments by zero, and the cap check `0 <= maxAssets` is trivially satisfied.

**Exploit flow**:
1. Maker posts a buy offer: `offer.buy = true`, `maxAssets = M`, tick chosen so `buyerPrice < WAD`.
2. Taker calls `take(1, ...)` repeatedly.
3. Each call: `buyerAssets = 0`, `consumed += 0`, check passes.
4. `creditOf(maker)` and `debtOf(taker)` grow with each call; `consumed` stays at its pre-call value.

**Why existing checks fail**: The only guard is `require(newConsumed <= offer.maxAssets)`. Since `newConsumed` is incremented by `0`, the check is trivially satisfied regardless of how many units have been delivered. The Certora rule `takeConsumedBoundedByMax` (line 62 of `certora/specs/Consume.spec`) only asserts `consumed <= maxAssets` — it does not verify that total units delivered are bounded by what `maxAssets` implies, so formal verification does not catch this. [2](#0-1) 

## Impact Explanation
A taker can fill a buy offer beyond the maker's `maxAssets` limit without advancing the consumed counter. The maker accumulates unbounded credit exposure they did not consent to, violating the core protocol invariant that offers cannot be overfilled. With `maxAssets = 1` and any tick below the maximum, the offer can be taken for arbitrarily many units at effectively zero token cost, giving the maker unbounded credit and the taker unbounded debt positions.

## Likelihood Explanation
Preconditions are: `offer.buy == true`, `offer.maxAssets > 0`, and `buyerPrice < WAD`. The last condition holds for virtually every real offer (it equals `WAD` only at the very top tick). The taker simply passes `units = 1`. No special privilege, oracle manipulation, or front-running is required. The attack is repeatable in a single transaction via `multicall`.

## Recommendation
Replace `mulDivDown` with `mulDivUp` when computing `buyerAssets` for the purpose of the consumed counter, or add an explicit guard:

```solidity
require(units == 0 || buyerAssets > 0, RoundingToZero());
```

This ensures that any non-trivial take advances the consumed counter by at least 1, making the cap enforceable.

## Proof of Concept
The named-bug regression test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` (lines 858–889) explicitly confirms this behavior: [3](#0-2) 

- Sets `consumed` to `maxAssets` (fully consumed) before the take.
- Calls `take(1, borrower, lenderOffer)` with `tick = MAX_TICK - 16` (so `buyerPrice < WAD`).
- Asserts `buyerAssets == 0` and `consumed` is unchanged (still equals `maxAssets`).
- Asserts `creditOf(lender)`, `debtOf(borrower)`, and `totalUnits` all strictly increased — confirming real state changes occurred despite the cap being exhausted.

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

**File:** certora/specs/Consume.spec (L59-63)
```text
rule takeConsumedBoundedByMax(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert offer.maxAssets > 0 => consumed(offer.maker, offer.group) <= offer.maxAssets;
    assert offer.maxAssets == 0 => consumed(offer.maker, offer.group) <= offer.maxUnits;
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
