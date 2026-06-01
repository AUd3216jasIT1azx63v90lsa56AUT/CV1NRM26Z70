The key check is at line 391 of `src/Midnight.sol`:

```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

When `block.timestamp == offer.market.maturity`, the left side of the `||` is `true` (equality satisfies `<=`), so the require passes unconditionally regardless of `sellerDebtIncrease`. The debt increase at line 414 (`sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease)`) then executes.

The `live_context.json` explicitly states: *"maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing"* — directly naming this exact case as forbidden.

All existing tests use strictly `maturity + 1` or `maturity - 1`; none cover the equality boundary.

---

### Title
Debt increase permitted at exact maturity timestamp due to `<=` boundary check - (File: src/Midnight.sol)

### Summary
The `take()` function guards against post-maturity debt increases with `require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity())`. The `<=` operator allows the check to pass when `block.timestamp == offer.market.maturity`, permitting `sellerDebtIncrease > 0` at the exact maturity boundary. This violates the protocol invariant that debt must not increase at or after maturity.

### Finding Description
**Code path:**

In `take()`, `timeToMaturity` is computed as `zeroFloorSub(offer.market.maturity, block.timestamp)`. [1](#0-0) 

When `block.timestamp == offer.market.maturity`, `timeToMaturity = 0`, so `settlementFee(id, 0)` returns the 0-day breakpoint fee. Units and prices are computed normally. [2](#0-1) 

Then `sellerDebtIncrease = units - sellerCreditDecrease` is computed (can be > 0 if seller has no credit). [3](#0-2) 

The maturity guard uses `<=`: [4](#0-3) 

At `block.timestamp == maturity`, `block.timestamp <= offer.market.maturity` is `true`, so the entire `||` expression is `true` and the require never reverts, even when `sellerDebtIncrease > 0`.

Debt is then written unconditionally: [5](#0-4) 

**Attacker inputs:** Any unprivileged taker can call `take()` with a valid sell offer (maker is seller) where the seller has no existing credit, at a block where `block.timestamp == offer.market.maturity`. No special privileges required — only a valid ratifier and a healthy seller position.

**Why existing checks fail:** The only maturity guard is the `<=` check at line 391. All other checks (expiry, tick, consumed limits, health) are orthogonal and do not prevent this. `TakeAmountsLib.buyerAssetsToUnits` mirrors the same `zeroFloorSub` computation, so it produces a consistent (non-reverting) unit count that feeds directly into the debt-increasing `take()` call. [6](#0-5) 

### Impact Explanation
At the exact maturity timestamp, a taker can cause `sellerPos.debt` to increase. This violates the core invariant that debt must not increase at or after maturity. A borrower who should be in a pure-repayment/settlement phase can have new debt minted against them, making their position worse and potentially enabling liquidation of a position that should have been protected from new debt.

### Likelihood Explanation
The precondition `block.timestamp == offer.market.maturity` is a single-block window that occurs exactly once per market. A taker monitoring the chain can submit a transaction in that block. The offer only needs a valid ratifier (e.g., a signature-based ratifier the maker already authorized). This is repeatable across any market whose maturity falls on a future block. It is not dependent on oracle values, admin action, or user error.

### Recommendation
Change the maturity guard from `<=` to `<`:

```solidity
// Before (line 391):
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());

// After:
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

This ensures that at `block.timestamp == maturity`, debt increase is forbidden, consistent with the documented invariant. [4](#0-3) 

### Proof of Concept
```solidity
function testDebtIncreaseAtExactMaturity() public {
    // Set block.timestamp == market.maturity exactly
    vm.warp(market.maturity);
    borrowerOffer.expiry = market.maturity; // offer valid at this timestamp
    uint256 units = 100;
    borrowerOffer.maxUnits = units;

    deal(address(loanToken), lender, units);
    collateralize(market, borrower, units); // borrower has no credit

    uint256 debtBefore = midnight.debtOf(id, borrower);

    // Should revert with CannotIncreaseDebtPostMaturity, but does NOT
    take(units, lender, borrowerOffer);

    uint256 debtAfter = midnight.debtOf(id, borrower);

    // This assertion FAILS — debt increased at maturity
    assertEq(debtAfter, debtBefore, "debt must not increase at maturity");
    // Or equivalently:
    // vm.expectRevert(IMidnight.CannotIncreaseDebtPostMaturity.selector);
    // take(units, lender, borrowerOffer); // does not revert — bug confirmed
}
```

Expected: revert with `CannotIncreaseDebtPostMaturity`. Actual: succeeds and increases `sellerPos.debt`. [7](#0-6)

### Citations

**File:** src/Midnight.sol (L359-360)
```text
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
        uint256 _settlementFee = settlementFee(id, timeToMaturity);
```

**File:** src/Midnight.sol (L383-384)
```text
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
```

**File:** src/Midnight.sol (L391-391)
```text
        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

**File:** src/Midnight.sol (L414-414)
```text
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/Midnight.sol (L963-979)
```text
    function settlementFee(bytes32 id, uint256 timeToMaturity) public view returns (uint256) {
        MarketState storage _marketState = marketState[id];
        require(_marketState.tickSpacing > 0, MarketNotCreated());

        if (timeToMaturity >= 360 days) return _marketState.settlementFeeCbp6 * CBP;

        // forgefmt: disable-start
        (uint256 start, uint256 end, uint256 feeLower, uint256 feeUpper) =
            timeToMaturity < 1 days   ? (  0 days,   1 days, _marketState.settlementFeeCbp0 * CBP, _marketState.settlementFeeCbp1 * CBP) :
            timeToMaturity < 7 days   ? (  1 days,   7 days, _marketState.settlementFeeCbp1 * CBP, _marketState.settlementFeeCbp2 * CBP) :
            timeToMaturity < 30 days  ? (  7 days,  30 days, _marketState.settlementFeeCbp2 * CBP, _marketState.settlementFeeCbp3 * CBP) :
            timeToMaturity < 90 days  ? ( 30 days,  90 days, _marketState.settlementFeeCbp3 * CBP, _marketState.settlementFeeCbp4 * CBP) :
            timeToMaturity < 180 days ? ( 90 days, 180 days, _marketState.settlementFeeCbp4 * CBP, _marketState.settlementFeeCbp5 * CBP) :
                                        (180 days, 360 days, _marketState.settlementFeeCbp5 * CBP, _marketState.settlementFeeCbp6 * CBP);
        // forgefmt: disable-end

        return (feeLower * (end - timeToMaturity) + feeUpper * (timeToMaturity - start)) / (end - start);
```

**File:** src/periphery/TakeAmountsLib.sol (L23-24)
```text
        uint256 settlementFee =
            IMidnight(midnight).settlementFee(id, UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp));
```

**File:** test/TakeTest.sol (L315-326)
```text
    function testBuy1PostMaturity() public {
        uint256 units = 100;
        uint256 timestamp = market.maturity + 1;
        vm.warp(timestamp);
        borrowerOffer.expiry = timestamp;
        borrowerOffer.maxUnits = units;
        deal(address(loanToken), lender, units);
        collateralize(market, borrower, units);

        vm.expectRevert(IMidnight.CannotIncreaseDebtPostMaturity.selector);
        take(units, lender, borrowerOffer);
    }
```
