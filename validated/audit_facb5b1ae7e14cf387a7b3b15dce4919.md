Audit Report

## Title
Off-by-one in maturity guard allows debt increase at `block.timestamp == maturity` - (File: src/Midnight.sol)

## Summary
`Midnight.take()` uses a non-strict `<=` comparison at line 391 to guard against post-maturity debt increases. At `block.timestamp == offer.market.maturity`, the guard evaluates to `true` and permits `sellerPos.debt` to be increased, directly violating the protocol's own stated invariant that "maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing." Any unprivileged taker can trigger this by submitting a take at the exact maturity block.

## Finding Description
**Root cause:** [1](#0-0) 

The condition `block.timestamp <= offer.market.maturity` is `true` when `block.timestamp == offer.market.maturity`, so the `require` passes and execution continues to: [2](#0-1) 

**Exploit flow:**
1. Maker creates a sell offer (`offer.buy = false`, maker is seller). An authorized operator calls `SetterRatifier.setIsRootRatified(maker, root, true)` to ratify the Merkle root containing the offer. [3](#0-2) 

2. Maker has no existing credit, so `sellerCreditDecrease = 0` and `sellerDebtIncrease = units` (line 383–384).
3. Any taker waits for `block.timestamp == offer.market.maturity` and calls `take(offer, ratifierData, units, taker, ...)` with a valid Merkle proof.
4. Line 391: `maturity <= maturity` → `true` → guard passes.
5. `sellerPos.debt += units` executes at line 414, increasing the maker's debt at exactly maturity.

**Why existing checks fail:**
- `block.timestamp <= offer.expiry` (line 353) passes if `offer.expiry >= maturity`, which is common for long-lived offers.
- The `isHealthy` check (line 476) only validates health after the take; it does not prevent the debt increase itself.
- All existing post-maturity tests bound `timestamp` to `market.maturity + 1`, leaving the exact boundary completely untested: [4](#0-3) [5](#0-4) 

## Impact Explanation
Debt is created at the exact moment the repayment window closes. The resulting position holds debt that is immediately overdue and liquidatable (per the protocol rule that overdue debt after maturity is liquidatable even if healthy), but was never repayable through normal pre-maturity flows. This constitutes bad debt creation and credit/debt accounting corruption — both listed as in-scope best bug classes. [6](#0-5) [7](#0-6) 

## Likelihood Explanation
Preconditions are standard protocol usage: a sell offer with a ratified root and `offer.expiry >= maturity`. `block.timestamp` on EVM is set by the block proposer, making exact-maturity execution straightforward — a taker simply submits the transaction targeting the maturity block. The condition is repeatable across every market whose sell offers have `expiry >= maturity`. No privileged access is required beyond the taker role, which is open to any address.

## Recommendation
Change the comparison at line 391 from `<=` to `<`:

```solidity
// Before
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());

// After
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

This aligns the guard with the protocol invariant and matches the intent of the existing `maturity_boundary_trade` attack flow scenario, which explicitly tests "warp to maturity" and asserts debt cannot increase. [8](#0-7) 

## Proof of Concept
Add the following test to `test/TakeTest.sol`, mirroring the structure of `testBuy1PostMaturity` but warping to exactly `market.maturity` instead of `market.maturity + 1`:

```solidity
function testSell1AtExactMaturity() public {
    uint256 units = 100;
    uint256 timestamp = market.maturity; // exact boundary, not +1
    vm.warp(timestamp);
    lenderOffer.expiry = timestamp;
    lenderOffer.maxUnits = units;
    deal(address(loanToken), lender, units);
    collateralize(market, borrower, units);

    // Should revert with CannotIncreaseDebtPostMaturity, but currently does NOT
    vm.expectRevert(IMidnight.CannotIncreaseDebtPostMaturity.selector);
    take(units, borrower, lenderOffer);
}
```

With the current `<=` guard this test fails (no revert occurs and `borrower.debt` is increased). After changing to `<`, the test passes.

### Citations

**File:** src/Midnight.sol (L391-391)
```text
        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

**File:** src/Midnight.sol (L414-414)
```text
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/ratifiers/SetterRatifier.sol (L24-27)
```text
    function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootRatified[maker][root] = newIsRootRatified;
        emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
```

**File:** test/TakeTest.sol (L315-318)
```text
    function testBuy1PostMaturity() public {
        uint256 units = 100;
        uint256 timestamp = market.maturity + 1;
        vm.warp(timestamp);
```

**File:** test/TakeTest.sol (L666-668)
```text
    function testBuyPastMaturity(uint256 timestamp) public {
        timestamp = bound(timestamp, market.maturity + 1, type(uint32).max);
        vm.warp(timestamp);
```

**File:** live_context.json (L196-198)
```json
      "selling units must reduce existing credit before increasing debt",
      "debt must not increase after maturity"
    ],
```

**File:** live_context.json (L219-222)
```json
    "maturity": [
      "pre-maturity and post-maturity paths must not be composable to bypass debt repayment",
      "maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing",
      "post-maturity unwinding must not mint free credit or erase debt incorrectly"
```

**File:** live_context.json (L264-274)
```json
    {
      "name": "maturity_boundary_trade",
      "sequence": [
        "open position before maturity",
        "warp to maturity - 1",
        "take/repay/settle",
        "warp to maturity",
        "try debt-increasing trade",
        "assert debt cannot increase after maturity"
      ]
    },
```
