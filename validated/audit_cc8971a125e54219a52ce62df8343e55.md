Audit Report

## Title
`maxAssets` Cap Bypass via Zero `buyerAssets` Rounding on Buy Offers with `buyerPrice < WAD` - (File: `src/Midnight.sol`)

## Summary
When `offer.buy = true` and `offer.maxAssets > 0`, the `take` function increments `consumed[maker][group]` by `buyerAssets = units.mulDivDown(buyerPrice, WAD)`. For any tick below `MAX_TICK`, `buyerPrice < WAD`, so `mulDivDown(1, buyerPrice, WAD) = 0`. Each such fill leaves `consumed` unchanged while still mutating credit, debt, and `totalUnits` state, allowing the `maxAssets` cap to be bypassed entirely. An attacker controlling both maker and taker addresses can accumulate unbounded lender credit without depositing any loan tokens.

## Finding Description

**Root cause — `src/Midnight.sol` lines 363–369:**

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // rounds DOWN → 0 when units < WAD/buyerPrice
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets()); // trivially passes when buyerAssets == 0
}
```

**Price bound:** The Certora rule `tickToPriceAtMostWad` (`certora/specs/TickToPrice.spec` line 48) proves `tickToPrice(tick) <= WAD` for all valid ticks. The rule `tickToPriceIsOneAtMaxTick` (line 38) proves equality holds only at `MAX_TICK = 5820`. For every tick below `MAX_TICK`, `offerPrice < WAD`, so `buyerPrice < WAD`. With `units = 1`: `mulDivDown(1, buyerPrice, WAD) = floor(buyerPrice / WAD) = 0`.

**Position state still mutates:** Lines 408–417 update `buyerPos.credit`, `sellerPos.debt`, and `_marketState.totalUnits` based on `units`, not `buyerAssets`. Line 455 transfers `buyerAssets - sellerAssets = 0` tokens. Credit is created with zero asset deposit.

**Protocol acknowledgment:** `src/Midnight.sol` line 94 states: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* The test `testBugBuyMaxAssetsBypass` (`test/TakeTest.sol` lines 857–889) explicitly demonstrates the bypass: sets `consumed = maxAssets`, takes with `units = 1`, asserts `buyerAssets == 0`, `consumed` unchanged, yet `creditOf`, `debtOf`, and `totalUnits` all strictly increased.

**Why existing checks fail:**
- `require(newConsumed <= offer.maxAssets)` cannot fire when the increment is zero.
- `SelfTake` blocks `offer.maker == taker` but not two coordinated addresses.
- The `makerFavorableRounding` Certora rule explicitly permits `buyerAssets = 0` as favorable to the maker, so no formal property catches this.

## Impact Explanation

An attacker controlling both a maker and a separate taker address can create an unbounded lender credit position (`buyerPos.credit`) without depositing any loan tokens. At maturity, this credit can be redeemed against the protocol's pool, draining funds from legitimate lenders. The `maxAssets` cap — the maker's primary commitment to limit total buyer-side spend — is rendered entirely ineffective for any buy offer at a non-par price (the entire usable price range except the single tick at `MAX_TICK`). Additionally, `claimableSettlementFee` accounting is corrupted: `buyerAssets - sellerAssets = 0` is added per fill despite real unit-level exposure being created.

## Likelihood Explanation

**Required preconditions:**
- `offer.buy = true`, `offer.maxAssets > 0`, `offer.tick < MAX_TICK` — satisfied by any non-par price, covering the entire practical price range.
- Attacker controls both the maker address (creates the offer) and a separate taker address (calls `take()`). This is a trivially low bar: two EOAs or an EOA plus a contract.
- The taker address must have sufficient collateral for the debt incurred.

The attack is repeatable indefinitely within a single block via `multicall`, and the tick condition is satisfied by the vast majority of real-world offers.

## Recommendation

Replace the `consumed` increment for buy offers with a unit-based guard when `buyerAssets` rounds to zero, or enforce a minimum `buyerAssets > 0` requirement before allowing a fill to proceed. Concretely, add a check such as:

```solidity
if (offer.maxAssets > 0) {
    uint256 increment = offer.buy ? buyerAssets : sellerAssets;
    require(increment > 0 || units == 0, ZeroAssetFill());
    newConsumed = consumed[offer.maker][offer.group] += increment;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, enforce that `units` must be large enough that `mulDivDown(units, buyerPrice, WAD) > 0` before proceeding, rejecting fills that would produce zero asset movement.

## Proof of Concept

The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` lines 857–889 is a complete, self-contained proof of concept. It:
1. Creates a buy offer with `maxAssets = 1` and `tick = MAX_TICK - 16` (price < WAD).
2. Pre-sets `consumed = maxAssets` (offer fully consumed).
3. Calls `take(1, borrower, lenderOffer)`.
4. Asserts `buyerAssets == 0`, `consumed` unchanged, yet `creditOf`, `debtOf`, and `totalUnits` all strictly increased.

Running `forge test --match-test testBugBuyMaxAssetsBypass` reproduces the bypass. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** src/Midnight.sol (L408-418)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);

        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

**File:** certora/specs/TickToPrice.spec (L38-49)
```text
rule tickToPriceIsOneAtMaxTick() {
    assert tickToPrice(maxTick()) == 10 ^ 18;
}

rule tickToPriceUsesPriceRoundingStep(uint256 tick) {
    assert tickToPrice(tick) % priceRoundingStep() == 0;
}

// Tick to price is at most 1e18.
// This notably ensures that offer prices are at most 1e18.
rule tickToPriceAtMostWad(uint256 tick) {
    assert tickToPrice(tick) <= 10 ^ 18;
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
