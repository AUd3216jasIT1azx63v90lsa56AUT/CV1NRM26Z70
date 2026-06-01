The critical check is at line 391 of `src/Midnight.sol`. Let me verify the exact boundary semantics and the invariant documentation.

### Title
Off-by-one in `CannotIncreaseDebtPostMaturity` guard allows new debt at `block.timestamp == maturity` - (`File: src/Midnight.sol`)

### Summary
The guard at `src/Midnight.sol:391` uses `block.timestamp <= offer.market.maturity`, making the condition true at the exact maturity timestamp and allowing `sellerDebtIncrease > 0` to pass unchecked. The protocol's own invariant document (`live_context.json:221`) explicitly names "timestamp equality" as a forbidden path for debt increase at the maturity boundary. All existing tests only cover `maturity + 1`, leaving the `== maturity` case untested and exploitable.

### Finding Description
**Code path:**

In `take()` (`src/Midnight.sol:337`), after computing `sellerDebtIncrease = units - sellerCreditDecrease` at line 384, the guard is:

```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

The logical OR means: if `block.timestamp <= offer.market.maturity` is true, the require passes regardless of `sellerDebtIncrease`. At `block.timestamp == market.maturity` this condition evaluates to `true`, so a non-zero `sellerDebtIncrease` is silently accepted.

**Attacker inputs and flow:**

1. Maker (unprivileged lender) creates a sell offer (`offer.buy = false`) with `offer.expiry >= market.maturity` and no existing credit in the market (so `sellerCreditDecrease = 0`, guaranteeing `sellerDebtIncrease = units`).
2. Maker calls `setIsRootRatified` on a `SetterRatifier` (`src/ratifiers/interfaces/ISetterRatifier.sol:20`) to ratify a Merkle root containing the offer — a fully unprivileged, permissionless action.
3. Taker waits until `block.timestamp == market.maturity` (achievable by a validator or by natural block timing when maturity aligns with a block).
4. Taker calls `take()`. The expiry check (`block.timestamp <= offer.expiry`) passes. The ratifier check passes. At line 391, `block.timestamp <= offer.market.maturity` is `true` (equality), so the require does not revert even though `sellerDebtIncrease = units > 0`.
5. Lines 414 and 416–417 execute: `sellerPos.debt += units` and `totalUnits += buyerCreditIncrease - sellerCreditDecrease = units`.

**Why existing checks fail:**

The `CannotIncreaseDebtPostMaturity` error is only triggered when `block.timestamp > offer.market.maturity` AND `sellerDebtIncrease != 0`. The `==` case is excluded by the `<=` operator. The `reduceOnly` flag is maker-controlled and not enforced by the protocol. No other check in `take()` blocks debt creation at exactly maturity.

### Impact Explanation
New debt units are written to `sellerPos.debt` and `totalUnits` is incremented at the exact maturity timestamp. The debt is immediately overdue (liquidatable) with zero repayment window. The corresponding credit increase (`buyerCreditIncrease`) represents a claim on loan tokens that must be backed by repayment of the newly created debt. If the debt becomes bad debt, `totalUnits` is inflated relative to the recoverable loan token balance, directly violating the solvency invariant: "contract token balances must cover withdrawable assets, collateral claims, credit redemptions, and accrued fees."

### Likelihood Explanation
The precondition requires `block.timestamp == market.maturity` exactly. On Ethereum, block timestamps are validator-controlled within a range; a validator or a searcher colluding with a validator can target a specific timestamp. Market maturities are set at creation and are publicly known, making them targetable. The offer setup (ratification via `setIsRootRatified`) is fully permissionless. The attack is repeatable across any market whose maturity aligns with a block timestamp.

### Recommendation
Change the comparison at `src/Midnight.sol:391` from `<=` to `<`:

```solidity
// Before (buggy):
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());

// After (fixed):
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

This aligns with the invariant at `live_context.json:221` ("maturity boundary must not allow debt increase by rounding, **timestamp equality**, or callback sequencing") and with the semantic that maturity is the last valid repayment moment, not a valid new-debt moment.

### Proof of Concept
```solidity
function testDebtIncreaseAtExactMaturity() public {
    uint256 units = 100;

    // Warp to EXACTLY maturity (not maturity + 1)
    vm.warp(market.maturity);

    // Sell offer: maker is lender (no existing credit), expiry == maturity
    lenderOffer.expiry = market.maturity;
    lenderOffer.maxUnits = units;
    lenderOffer.tick = MAX_TICK;

    // Fund buyer (taker = borrower)
    uint256 price = TickLib.tickToPrice(MAX_TICK);
    deal(address(loanToken), borrower, units.mulDivUp(price, WAD));
    collateralize(market, borrower, units);

    uint256 debtBefore = midnight.debtOf(id, lender);
    uint256 totalUnitsBefore = midnight.totalUnits(id);

    // This should revert with CannotIncreaseDebtPostMaturity but does NOT
    take(units, borrower, lenderOffer);

    // These assertions PASS, proving the invariant is broken:
    assertGt(midnight.debtOf(id, lender), debtBefore, "debt increased at maturity");
    assertGt(midnight.totalUnits(id), totalUnitsBefore, "totalUnits inflated at maturity");
}
```

Expected: revert with `CannotIncreaseDebtPostMaturity`.
Actual: call succeeds, `lender.debt == units`, `totalUnits` increased by `units`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** src/Midnight.sol (L382-384)
```text
        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
```

**File:** src/Midnight.sol (L391-391)
```text
        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

**File:** src/Midnight.sol (L413-417)
```text
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);

        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
```

**File:** live_context.json (L219-222)
```json
    "maturity": [
      "pre-maturity and post-maturity paths must not be composable to bypass debt repayment",
      "maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing",
      "post-maturity unwinding must not mint free credit or erase debt incorrectly"
```

**File:** src/ratifiers/interfaces/ISetterRatifier.sol (L20-20)
```text
    function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) external;
```

**File:** test/TakeTest.sol (L315-339)
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

    function testSell1PostMaturity() public {
        uint256 units = 100;
        uint256 timestamp = market.maturity + 1;
        vm.warp(timestamp);
        lenderOffer.expiry = timestamp;
        lenderOffer.maxUnits = units;
        deal(address(loanToken), lender, units);
        collateralize(market, borrower, units);

        vm.expectRevert(IMidnight.CannotIncreaseDebtPostMaturity.selector);
        take(units, borrower, lenderOffer);
    }
```
