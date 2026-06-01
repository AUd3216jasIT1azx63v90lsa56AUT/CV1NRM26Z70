### Title
Fully-consumed assets-mode buy offer with `offerPrice < WAD` allows unbounded credit/debt inflation via zero-`buyerAssets` rounding bypass - (`src/Midnight.sol`)

### Summary

When a buy offer uses `maxAssets` mode and `offerPrice < WAD`, `buyerAssets` is computed via `mulDivDown`, which truncates to zero for sufficiently small `units`. The consumed-cap check increments `consumed` by `buyerAssets` (zero), so the check `newConsumed <= offer.maxAssets` passes even when `consumed` is already at `maxAssets`. Position state (credit, debt, `totalUnits`) is updated using `units` (nonzero), not `buyerAssets`, so real economic state changes occur with zero asset transfer and zero consumed increment. This is repeatable without bound.

### Finding Description

**Root cause — dual accounting split between `buyerAssets` and `units`:**

In `src/Midnight.sol` line 363, `buyerAssets` is computed with `mulDivDown`:

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : ...;
```

When `buyerPrice < WAD` (i.e., `offerPrice < WAD`) and `units` is small enough that `units * buyerPrice < WAD`, `buyerAssets` truncates to `0`. [1](#0-0) 

The consumed-cap branch at line 368 increments `consumed` by `buyerAssets` (zero) and checks the result:

```solidity
newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

If `consumed[maker][group]` is already at `maxAssets`, adding zero leaves `newConsumed == maxAssets`, and `maxAssets <= maxAssets` passes. [2](#0-1) 

Position state is then updated using `units` (nonzero), not `buyerAssets`:

```solidity
uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
...
buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);
sellerPos.debt   += UtilsLib.toUint128(sellerDebtIncrease);
_marketState.totalUnits += buyerCreditIncrease - sellerCreditDecrease;
``` [3](#0-2) 

Token transfers use `buyerAssets` and `sellerAssets` (both zero), so no tokens move:

```solidity
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets); // 0
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);                   // 0
``` [4](#0-3) 

**Exploit flow:**

1. Maker (lender) creates a buy offer with `maxAssets = N`, `maxUnits = 0`, `tick` set such that `offerPrice < WAD`.
2. Attacker (or anyone) calls `setConsumed` or takes normally until `consumed[maker][group] == maxAssets`.
3. Attacker calls `take(units=1, borrower, lenderOffer)` — `buyerAssets = 1.mulDivDown(buyerPrice, WAD) = 0`.
4. Consumed check: `maxAssets + 0 <= maxAssets` → passes.
5. `buyerCreditIncrease = 1`, `sellerDebtIncrease = 1` → maker's credit increases by 1, taker's debt increases by 1, `totalUnits` increases by 1.
6. No tokens transferred. `consumed` unchanged.
7. Step 3–6 is repeatable indefinitely.

**Why existing checks fail:**

- The `ConsumedAssets` check (line 369) only guards against `consumed` exceeding `maxAssets`, but the increment is `buyerAssets` (zero), not `units`. There is no check that `units == 0` when `buyerAssets == 0`.
- The Certora rule `takeConsumedAtMaxUnchangedAssets` (line 88–97 of `certora/specs/Consume.spec`) only asserts that `consumed` is unchanged when already at max — it does not assert that credit, debt, or `totalUnits` are unchanged. [5](#0-4) 
- The protocol comment at line 94 of `src/Midnight.sol` explicitly acknowledges this as a known behavior: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* [6](#0-5) 

### Impact Explanation

The maker's credit grows beyond their intended `maxAssets` cap with zero asset inflow. The taker accumulates debt without receiving any assets. Since `consumed` never increments, the attack is repeatable without limit: each call with `units = 1` adds 1 unit of credit to the maker and 1 unit of debt to the taker at zero cost. This breaks the invariant that a fully-consumed offer admits only no-op takes (the units-mode analogue `fullyConsumedOfferRevertsOnNonTrivialTake` enforces this for units mode but has no equivalent for assets mode with `buyerAssets = 0`). The maker's credit is not backed by any asset payment, and the taker's debt has no corresponding asset receipt, corrupting the credit/debt accounting. [7](#0-6) 

### Likelihood Explanation

Preconditions are all attacker-controllable: the offer tick must satisfy `offerPrice < WAD` (any tick below the WAD-equivalent tick), `maxAssets > 0`, `maxUnits = 0`, and `consumed >= maxAssets`. The attacker can set `consumed` to `maxAssets` themselves via `setConsumed` (permissionless for their own group) or by taking normally. The call requires only that the taker is authorized and the ratifier approves — standard conditions. The attack is repeatable in every block with no cost beyond gas (zero token transfer). Any taker can execute this against any qualifying buy offer.

### Recommendation

Add a guard that when `maxAssets > 0` and `offer.buy`, require that `buyerAssets > 0` whenever `units > 0`, or equivalently require `units == 0` when `buyerAssets == 0`:

```solidity
if (offer.maxAssets > 0) {
    if (offer.buy) require(units == 0 || buyerAssets > 0, ZeroBuyerAssets());
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

This mirrors the units-mode invariant (where `units == 0` is the only no-op) and closes the gap between the consumed increment and the position-state update.

### Proof of Concept

The test `testBugBuyMaxAssetsBypass` already exists in `test/TakeTest.sol` and passes, confirming the bug: [8](#0-7) 

```solidity
// test/TakeTest.sol — testBugBuyMaxAssetsBypass (lines 858–889)
function testBugBuyMaxAssetsBypass() public {
    deal(address(loanToken), lender, 0);       // lender pays 0
    collateralize(market, borrower, 100);

    lenderOffer.maxUnits  = 0;
    lenderOffer.maxAssets = 1;
    lenderOffer.tick      = MAX_TICK - 16;     // offerPrice < WAD

    vm.prank(lender);
    midnight.setConsumed(lenderOffer.group, lenderOffer.maxAssets, lender); // fully consume

    uint256 lenderCreditBefore  = midnight.creditOf(id, lender);
    uint256 borrowerDebtBefore  = midnight.debtOf(id, borrower);
    uint256 totalUnitsBefore    = midnight.totalUnits(id);

    (uint256 buyerAssets, uint256 sellerAssets) = take(1, borrower, lenderOffer);

    assertEq(buyerAssets,  0);  // zero assets transferred
    assertEq(sellerAssets, 0);

    // consumed unchanged — cap check was bypassed
    assertEq(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets);

    // but position state changed — the bug
    assertGt(midnight.creditOf(id, lender),  lenderCreditBefore);   // maker credit grew
    assertGt(midnight.debtOf(id, borrower),  borrowerDebtBefore);   // taker debt grew
    assertGt(midnight.totalUnits(id),        totalUnitsBefore);      // totalUnits grew
}
```

**Extended fuzz/invariant test idea:** wrap the above in a loop calling `take(1, borrower, lenderOffer)` N times and assert `creditOf(lender) == lenderCreditBefore + N` while `consumed(lender, group) == maxAssets` throughout, demonstrating unbounded inflation.

### Citations

**File:** src/Midnight.sol (L93-94)
```text
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L363-363)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
```

**File:** src/Midnight.sol (L367-369)
```text
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L382-417)
```text
        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
        uint128 buyerPendingFeeIncrease =
            UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
        uint128 sellerPendingFeeDecrease = sellerPos.credit > 0
            ? UtilsLib.toUint128(sellerPos.pendingFee.mulDivUp(sellerCreditDecrease, sellerPos.credit))
            : 0;

        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
        require(
            !offer.reduceOnly || (offer.buy ? buyerCreditIncrease == 0 : sellerDebtIncrease == 0),
            MakerCreditOrDebtIncreased()
        );

        require(
            offer.market.enterGate == address(0) || buyerCreditIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseCredit(buyer),
            BuyerGatedFromIncreasingCredit()
        );
        require(
            offer.market.enterGate == address(0) || sellerDebtIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseDebt(seller),
            SellerGatedFromIncreasingDebt()
        );

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

**File:** certora/specs/Consume.spec (L99-111)
```text
/// A fully-consumed offer in units mode only allows no-op takes.
rule fullyConsumedOfferRevertsOnNonTrivialTake(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxAssets == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    require offer.maxUnits > 0 && consumedBefore >= offer.maxUnits, "assume the offer is fully consumed";

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    // If take does not revert, its input has to be zero.
    assert units == 0;
}
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
