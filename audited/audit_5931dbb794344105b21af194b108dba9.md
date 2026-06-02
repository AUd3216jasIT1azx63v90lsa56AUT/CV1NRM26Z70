Audit Report

## Title
Off-by-one in maturity guard allows debt increase at `block.timestamp == maturity` - (File: src/Midnight.sol)

## Summary
`Midnight.take()` uses `block.timestamp <= offer.market.maturity` at line 391 to guard against post-maturity debt increases. When `block.timestamp == offer.market.maturity`, this condition evaluates to `true`, permitting `sellerPos.debt` to increase. This directly violates the protocol's own stated invariant: "maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing." Any unprivileged taker can trigger this by submitting a take at the exact maturity block.

## Finding Description

**Root cause:** Line 391 uses a non-strict `<=` comparison:

```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

When `block.timestamp == offer.market.maturity`, the first disjunct is `true`, the `require` passes, and execution reaches line 414:

```solidity
sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

This is confirmed as a violation by the protocol's own `live_context.json` invariant list under `core_invariants.maturity`: "maturity boundary must not allow debt increase by rounding, **timestamp equality**, or callback sequencing," and under `core_invariants.credit_debt`: "debt must not increase after maturity." The `whitepaper_constants_and_rules.maturity` field also states `"debt_increase_after_maturity_forbidden": true`.

**Exploit flow:**
1. Maker creates a sell offer (`offer.buy = false`, maker is seller) with `offer.expiry >= maturity` (standard for long-lived offers). A ratifier (e.g., `SetterRatifier`) ratifies the Merkle root containing the offer.
2. Maker has no existing credit, so `sellerCreditDecrease = 0` and `sellerDebtIncrease = units` (lines 383–384).
3. Taker waits for `block.timestamp == offer.market.maturity` and calls `take(offer, ratifierData, units, taker, ...)` with a valid Merkle proof.
4. Line 391: `maturity <= maturity` → `true` → guard passes.
5. `sellerPos.debt += units` executes at line 414, increasing the maker's debt at exactly maturity.
6. `timeToMaturity = zeroFloorSub(maturity, maturity) = 0`, so `buyerPendingFeeIncrease = 0` — the buyer receives credit with zero pending fee.

**Why existing checks fail:**
- `block.timestamp <= offer.expiry` (line 353) passes if `offer.expiry >= maturity`, which is the common case.
- The `isHealthy` check (line 476) only validates health after the take; it does not prevent the debt increase itself.
- Post-maturity liquidation requires `block.timestamp > market.maturity` (strict, line 622), so at exactly `block.timestamp == maturity`, the newly created debt cannot be liquidated via post-maturity mode — it is only liquidatable if the position is also unhealthy.
- All existing post-maturity tests bound `timestamp` to `market.maturity + 1`, leaving the exact boundary completely untested (e.g., `testBuy1PostMaturity`, `testSell1PostMaturity`, `testBuyPastMaturity`, `testSellPastMaturity`).

The protocol's own `high_value_attack_flows` entry `maturity_boundary_trade` explicitly lists "warp to maturity" → "try debt-increasing trade" → "assert debt cannot increase after maturity" as a required test case, confirming the protocol designers intended this to revert.

## Impact Explanation
Debt is created at the exact moment the repayment window closes. The resulting position holds debt that is immediately overdue: post-maturity liquidation becomes available at the very next block (`block.timestamp > maturity`), with LIF starting at 1 and ramping to `maxLif` over `TIME_TO_MAX_LIF`. The debt was created with `timeToMaturity = 0`, so the buyer receives credit with zero pending fee — a fee accounting inconsistency. This constitutes bad debt creation and credit/debt accounting corruption, both listed as in-scope best bug classes. The invariant "debt must not increase after maturity" is broken at the exact boundary.

## Likelihood Explanation
Preconditions are standard protocol usage: a sell offer with a ratified root and `offer.expiry >= maturity`. `block.timestamp` on EVM is set by the block proposer, making exact-maturity execution straightforward — a taker submits the transaction targeting the maturity block. The condition is repeatable across every market whose sell offers have `expiry >= maturity`. No privileged access is required beyond the taker role, which is open to any address.

## Recommendation
Change the comparison at line 391 from `<=` to `<`:

```solidity
// Before (vulnerable):
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());

// After (fixed):
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

This ensures that at `block.timestamp == maturity`, the first disjunct is `false`, and the require only passes if `sellerDebtIncrease == 0` (i.e., no new debt is created). This aligns with the stated invariant and the `high_value_attack_flows.maturity_boundary_trade` test expectation.

## Proof of Concept
Minimal Foundry test (add to `TakeTest.sol`):

```solidity
function testSellAtExactMaturity() public {
    uint256 units = 100;
    uint256 timestamp = market.maturity; // exact boundary
    vm.warp(timestamp);
    lenderOffer.expiry = timestamp;      // offer valid up to and including maturity
    lenderOffer.maxUnits = units;
    deal(address(loanToken), lender, units);
    collateralize(market, borrower, units);

    // Should revert with CannotIncreaseDebtPostMaturity, but currently does NOT
    vm.expectRevert(IMidnight.CannotIncreaseDebtPostMaturity.selector);
    take(units, borrower, lenderOffer);
}
```

With the current `<=` guard, this test **fails** (no revert occurs and `borrower.debt` increases). After changing to `<`, the test passes. This mirrors the existing `testSell1PostMaturity` pattern but at `maturity` instead of `maturity + 1`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** src/Midnight.sol (L391-391)
```text
        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

**File:** src/Midnight.sol (L412-414)
```text
        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
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

**File:** test/TakeTest.sol (L666-690)
```text
    function testBuyPastMaturity(uint256 timestamp) public {
        timestamp = bound(timestamp, market.maturity + 1, type(uint32).max);
        vm.warp(timestamp);
        borrowerOffer.expiry = timestamp;
        borrowerOffer.maxUnits = 100;
        borrowerOffer.tick = MAX_TICK;
        deal(address(loanToken), lender, 100);
        collateralize(market, borrower, 100);

        vm.expectRevert(IMidnight.CannotIncreaseDebtPostMaturity.selector);
        take(100, lender, borrowerOffer);
    }

    function testSellPastMaturity(uint256 timestamp) public {
        timestamp = bound(timestamp, market.maturity + 1, type(uint32).max);
        vm.warp(timestamp);
        lenderOffer.expiry = timestamp;
        lenderOffer.maxUnits = 100;
        lenderOffer.tick = MAX_TICK;
        deal(address(loanToken), lender, 100);
        collateralize(market, borrower, 100);

        vm.expectRevert(IMidnight.CannotIncreaseDebtPostMaturity.selector);
        take(100, borrower, lenderOffer);
    }
```

**File:** live_context.json (L84-89)
```json
    "maturity": {
      "trading_after_maturity_allowed": true,
      "debt_increase_after_maturity_forbidden": true,
      "post_maturity_trading_purpose": "facilitate unwinding when liquidations are unprofitable",
      "overdue_debt_after_maturity_is_liquidatable_even_if_healthy": true
    },
```

**File:** live_context.json (L219-223)
```json
    "maturity": [
      "pre-maturity and post-maturity paths must not be composable to bypass debt repayment",
      "maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing",
      "post-maturity unwinding must not mint free credit or erase debt incorrectly"
    ],
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
