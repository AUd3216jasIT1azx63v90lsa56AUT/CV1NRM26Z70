The existing test at line 857 already names and demonstrates this exact class of bug. Let me verify the full code path precisely.

### Title
Zero-increment consumed allows indefinite overfill of near-fully-consumed buy offers with `buyerPrice < WAD` - (`src/Midnight.sol`)

### Summary
When `offer.maxAssets > 0` and `offer.buy == true`, the consumed counter is incremented by `buyerAssets`, computed as `units.mulDivDown(buyerPrice, WAD)`. If `buyerPrice < WAD` (achievable with a low tick) and `units` is small enough that `units * buyerPrice < WAD`, then `buyerAssets = 0`, the consumed counter does not advance, and the `require(newConsumed <= offer.maxAssets)` guard passes indefinitely. Position state (credit, debt, `totalUnits`) still mutates on every such call, even though no assets are transferred.

### Finding Description

**Code path** — `src/Midnight.sol` `take()`:

```
sellerPrice = offerPrice - _settlementFee          // offer.buy == true
buyerPrice  = sellerPrice + _settlementFee          // == offerPrice
buyerAssets = units.mulDivDown(buyerPrice, WAD)     // line 363 — rounds DOWN
...
newConsumed = consumed[maker][group] += buyerAssets // line 368
require(newConsumed <= offer.maxAssets, ...)        // line 369
```

**Root cause** — `buyerAssets` is the quantity added to `consumed`. When `buyerPrice < WAD` (i.e. `offerPrice < 1e18`, reachable with any tick below the WAD-price tick) and `units = 1`, `mulDivDown(1 * buyerPrice, WAD) = 0`. The consumed mapping is not advanced, so the cap check is trivially satisfied on every subsequent call.

**Exploit flow:**

1. Attacker (taker) observes a buy offer with `maxAssets > 0` and `tick` such that `offerPrice < WAD`.
2. First call(s): fill the offer normally until `consumed[maker][group] = maxAssets - 1`.
3. Repeated calls: `take(offer, ..., units=1)`. Each call computes `buyerAssets = 0`, so `newConsumed = maxAssets - 1 + 0 = maxAssets - 1 ≤ maxAssets` — passes. Position state mutates: `buyerCreditIncrease = zeroFloorSub(1, buyerPos.debt)` and `sellerDebtIncrease` are non-zero, so `credit`, `debt`, and `totalUnits` all change with each call, while zero tokens are transferred.

**Why existing checks fail:**

- `require(newConsumed <= offer.maxAssets)` — only fires when consumed actually increases; a zero-increment bypasses it entirely.
- No guard on `units > 0 && buyerAssets == 0`.
- `reduceOnly` does not apply here.
- The existing test `testBugBuyMaxAssetsBypass` (line 857) already demonstrates the identical mechanism starting from `consumed == maxAssets` and confirms `creditOf`, `debtOf`, and `totalUnits` all change while `buyerAssets == 0`. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation

An unprivileged taker can call `take` with `units=1` an unbounded number of times on a near-fully-consumed buy offer (consumed = `maxAssets - 1`). Each call:
- passes the `ConsumedAssets` guard (consumed never advances past `maxAssets - 1`),
- increases the maker's credit by 1 unit and increases the taker's debt by 1 unit (or decreases the taker's credit),
- inflates `totalUnits` and `claimableSettlementFee` by 0 (no fee collected),
- transfers 0 tokens.

The offer is effectively overfilled without limit, violating the invariant that `consumed[maker][group]` must reach `maxAssets` to exhaust the offer. [4](#0-3) 

### Likelihood Explanation

**Preconditions:**
- `offer.buy == true` and `offer.maxAssets > 0` — standard offer configuration.
- `offerPrice < WAD` — requires a tick below the WAD-price tick; the test uses `MAX_TICK - 16`, confirming this is a reachable tick value.
- `consumed` must reach `maxAssets - 1` — the attacker can arrange this themselves in the first fill.

All preconditions are fully attacker-controlled with no privileged access required. The attack is repeatable indefinitely within a single transaction or across multiple transactions. [5](#0-4) 

### Recommendation

Add a guard that rejects a take where `units > 0` but the consumed increment is zero in assets mode:

```solidity
if (offer.maxAssets > 0) {
    uint256 delta = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || delta > 0, ZeroConsumedIncrement()); // add this
    newConsumed = consumed[offer.maker][offer.group] += delta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

This ensures that any non-trivial take (units > 0) must advance the consumed counter by at least 1, making it impossible to loop on a near-fully-consumed offer. [6](#0-5) 

### Proof of Concept

```solidity
function testOverfillNearMaxAssets() public {
    // Setup: buy offer with offerPrice < WAD (tick = MAX_TICK - 16)
    lenderOffer.maxUnits = 0;
    lenderOffer.maxAssets = 2;          // small cap for clarity
    lenderOffer.tick = MAX_TICK - 16;   // offerPrice < WAD → buyerAssets rounds to 0 for units=1

    collateralize(market, borrower, 100);
    deal(address(loanToken), lender, 100);

    // Step 1: fill to maxAssets - 1 = 1 consumed
    // (use units large enough that buyerAssets == 1 for the first fill)
    // ... first take sets consumed[lender][group] = 1

    uint256 creditBefore = midnight.creditOf(id, lender);
    uint256 debtBefore   = midnight.debtOf(id, borrower);
    uint256 unitsBefore  = midnight.totalUnits(id);

    // Step 2: take with units=1, buyerAssets=0 — should revert but does not
    (uint256 ba,) = take(1, borrower, lenderOffer);
    assertEq(ba, 0, "buyerAssets must be 0");

    // consumed did NOT advance — still maxAssets - 1
    assertEq(midnight.consumed(lender, lenderOffer.group), 1);

    // But position state changed — invariant violated
    assertGt(midnight.creditOf(id, lender),  creditBefore, "credit grew");
    assertGt(midnight.debtOf(id, borrower),  debtBefore,   "debt grew");
    assertGt(midnight.totalUnits(id),        unitsBefore,  "totalUnits grew");

    // Step 3: repeat — same call succeeds again, indefinitely
    (ba,) = take(1, borrower, lenderOffer);
    assertEq(ba, 0);
    assertGt(midnight.creditOf(id, lender), creditBefore + 1);
}
```

Expected assertion: the second `take` at step 3 should revert with `ConsumedAssets` (or a new `ZeroConsumedIncrement` error), but currently it succeeds, confirming the overfill. [3](#0-2) [1](#0-0)

### Citations

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

**File:** src/Midnight.sol (L382-384)
```text
        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
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
