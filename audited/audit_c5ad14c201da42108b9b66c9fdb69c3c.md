The key facts are now clear. Let me analyze the exact code path.

**Critical check at `src/Midnight.sol:391`:**
```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

Uses `<=` (inclusive). At `block.timestamp == market.maturity`, the condition is `true`, so debt increase passes.

**Expiry check at `src/Midnight.sol:353`:**
```solidity
require(block.timestamp <= offer.expiry, OfferExpired());
```

If `offer.expiry >= market.maturity` and `block.timestamp == market.maturity`, this also passes.

**Stated invariant from `live_context.json:221`:**
```
"maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing"
```

Explicitly prohibits timestamp equality from permitting debt increase. The code violates this.

**`EcrecoverRatifier.isRatified`** (lines 33–46): only checks signature, Merkle proof, and root cancellation — no maturity logic. It does not block this path.

**Test coverage gap:** All post-maturity tests (`testBuy1PostMaturity`, `testBuyPastMaturity`, `testSell1PostMaturity`, `testSellPastMaturity`) warp to `market.maturity + 1`. None test `block.timestamp == market.maturity` with a debt-increasing fill.

---

### Title
Off-by-one `<=` in `CannotIncreaseDebtPostMaturity` guard allows debt increase at exact maturity timestamp - (`src/Midnight.sol`)

### Summary
The `take()` function guards against post-maturity debt increases with `block.timestamp <= offer.market.maturity`, which is inclusive of the maturity timestamp itself. The protocol's own invariant explicitly states that "timestamp equality" must not allow debt increase. At exactly `block.timestamp == market.maturity`, an unprivileged taker can fill a sell offer and increase the seller's debt, creating immediately-overdue, liquidatable debt that the borrower cannot repay before maturity.

### Finding Description
**Code path:**

`Midnight.take()` → line 353 expiry check → line 391 maturity guard.

At `block.timestamp == market.maturity`:

1. `require(block.timestamp <= offer.expiry, OfferExpired())` — passes if `offer.expiry >= market.maturity` (any non-expired offer).
2. `sellerDebtIncrease = units - sellerCreditDecrease` — computed at lines 383–384; positive when seller has no offsetting credit.
3. `require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity())` — at line 391, `block.timestamp <= offer.market.maturity` evaluates to `maturity <= maturity` = `true`, so the require passes regardless of `sellerDebtIncrease`.
4. `sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease)` — line 414 executes, increasing the seller's debt.

**Root cause:** `<=` instead of `<` in the maturity guard. The protocol's stated invariant (`live_context.json:221`) explicitly names "timestamp equality" as a forbidden bypass vector, but the implementation uses an inclusive comparison.

**Attacker inputs:** Any valid sell offer (borrower as maker) with `offer.expiry >= market.maturity`, called by an unprivileged taker at `block.timestamp == market.maturity` with `units > sellerPos.credit`.

**Why existing checks fail:** The `OfferExpired` check at line 353 also uses `<=`, so it passes at equality. The `CannotIncreaseDebtPostMaturity` check at line 391 uses `<=`, so it passes at equality. No other check in `take()` prevents this. `EcrecoverRatifier.isRatified` performs only signature/Merkle validation with no maturity awareness. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

### Impact Explanation
At exactly `block.timestamp == market.maturity`, a taker can increase a borrower's debt. Since `block.timestamp >= market.maturity` makes overdue debt immediately liquidatable, the borrower acquires new debt that is liquidatable in the same block it is created. The borrower cannot repay before maturity because maturity has already arrived. This is a concrete griefing path: the taker forces the borrower into a larger liquidatable position than the borrower intended to hold at maturity, potentially causing unexpected collateral seizure. [5](#0-4) 

### Likelihood Explanation
**Preconditions:**
- A sell offer exists with `offer.expiry >= market.maturity` (common — makers routinely set expiry at or beyond maturity).
- The taker submits the transaction in the block where `block.timestamp == market.maturity`.

**Feasibility:** On EVM chains, block timestamps are set by validators/miners and advance monotonically. The maturity timestamp is a known, fixed value. A taker (or MEV bot) can monitor the mempool and submit the fill transaction targeting the maturity block. This is a 1-block window but is deterministic and repeatable for every market at its maturity timestamp.

**Repeatability:** Every market with a sell offer whose expiry reaches maturity is vulnerable at its maturity block. [1](#0-0) 

### Recommendation
Change the maturity guard from `<=` to `<` (strict less-than):

```solidity
// Before (line 391):
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());

// After:
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

This aligns the implementation with the stated invariant that "timestamp equality" must not permit debt increase. The `OfferExpired` check at line 353 can remain `<=` since offer expiry is a maker-controlled deadline unrelated to the protocol's maturity semantics. [1](#0-0) 

### Proof of Concept
```solidity
function testDebtIncreaseAtExactMaturity() public {
    uint256 units = 100;
    // Warp to exactly maturity (not maturity + 1)
    uint256 maturity = market.maturity;
    vm.warp(maturity);

    // Sell offer from borrower with expiry == maturity
    borrowerOffer.expiry = maturity;
    borrowerOffer.maxUnits = units;
    collateralize(market, borrower, units);
    deal(address(loanToken), lender, units);

    uint256 debtBefore = midnight.debtOf(id, borrower);

    // Should revert with CannotIncreaseDebtPostMaturity but does NOT
    take(units, lender, borrowerOffer);

    uint256 debtAfter = midnight.debtOf(id, borrower);

    // Assert: debt increased at maturity — violates invariant
    assertGt(debtAfter, debtBefore, "debt must not increase at maturity");

    // Assert: borrower is immediately liquidatable (overdue debt)
    assertFalse(midnight.isHealthy(market, id, borrower), "borrower should be liquidatable");
}
```

Expected behavior with the bug: `take()` succeeds, `debtAfter > debtBefore`, borrower is immediately liquidatable. Expected behavior after fix: `take()` reverts with `CannotIncreaseDebtPostMaturity`. [1](#0-0) [2](#0-1)

### Citations

**File:** src/Midnight.sol (L353-353)
```text
        require(block.timestamp <= offer.expiry, OfferExpired());
```

**File:** src/Midnight.sol (L382-384)
```text
        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
```

**File:** src/Midnight.sol (L391-414)
```text
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
```
