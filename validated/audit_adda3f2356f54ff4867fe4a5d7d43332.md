The code at line 391 confirms the claim exactly: [1](#0-0) 

```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

The `<=` operator makes the left side `true` at `block.timestamp == maturity`, bypassing the `sellerDebtIncrease == 0` check entirely.

The protocol's own invariant document explicitly names this as forbidden: [2](#0-1) 

At `block.timestamp == maturity`, `timeToMaturity = zeroFloorSub(maturity, maturity) = 0` (line 359), so `buyerPendingFeeIncrease = 0` — the debt is created with no fee, making the attack more attractive. [3](#0-2) [4](#0-3) 

The state writes at lines 414 and 416–417 execute unconditionally once the guard passes.

---

Audit Report

## Title
Off-by-one in `CannotIncreaseDebtPostMaturity` guard allows new debt at `block.timestamp == maturity` - (File: src/Midnight.sol)

## Summary
The guard at `src/Midnight.sol:391` uses `block.timestamp <= offer.market.maturity`, which evaluates to `true` at the exact maturity timestamp, allowing `sellerDebtIncrease > 0` to pass unchecked. This directly contradicts the protocol's own invariant (`live_context.json:221`) that explicitly forbids debt increase via "timestamp equality" at the maturity boundary. Debt created at this instant is immediately overdue with zero repayment window, inflating `totalUnits` and creating bad debt risk.

## Finding Description
In `take()` (`src/Midnight.sol:337`), after computing `sellerDebtIncrease = units - sellerCreditDecrease` at line 384, the guard is:

```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

The logical OR short-circuits: when `block.timestamp == offer.market.maturity`, the left operand is `true`, so the require passes regardless of `sellerDebtIncrease`. The intended invariant — no new debt at or after maturity — is only enforced for `block.timestamp > maturity`.

**Exploit flow:**
1. Maker creates a sell offer (`offer.buy = false`) with `offer.expiry >= market.maturity` and no existing credit in the market (ensuring `sellerCreditDecrease = 0`, so `sellerDebtIncrease = units`).
2. Maker ratifies the offer via `setIsRootRatified` on a `SetterRatifier` — a fully permissionless action.
3. Taker waits until `block.timestamp == market.maturity` (achievable by a validator or natural block alignment).
4. Taker calls `take()`. The expiry check (`block.timestamp <= offer.expiry`) passes. At line 391, `block.timestamp <= offer.market.maturity` is `true` (equality), so the require does not revert even with `sellerDebtIncrease = units > 0`.
5. Lines 414 and 416–417 execute: `sellerPos.debt += units` and `totalUnits += buyerCreditIncrease - sellerCreditDecrease`.

**Why existing checks fail:**
- `CannotIncreaseDebtPostMaturity` only triggers when `block.timestamp > maturity` AND `sellerDebtIncrease != 0`; the `==` case is excluded by `<=`.
- `offer.reduceOnly` is maker-controlled and not enforced by the protocol.
- `timeToMaturity = 0` at this point (line 359), so `buyerPendingFeeIncrease = 0` — no fee is charged, making the attack costless.
- No other check in `take()` blocks debt creation at exactly maturity.

## Impact Explanation
New debt units are written to `sellerPos.debt` and `totalUnits` is incremented at the exact maturity timestamp. The debt is immediately overdue (liquidatable) with zero repayment window. The corresponding `buyerCreditIncrease` represents a claim on loan tokens that must be backed by repayment of the newly created debt. If the debt becomes bad debt, `totalUnits` is inflated relative to the recoverable loan token balance, directly violating the solvency invariant: contract token balances must cover withdrawable assets, collateral claims, credit redemptions, and accrued fees.

## Likelihood Explanation
The precondition requires `block.timestamp == market.maturity` exactly. On Ethereum, block timestamps are validator-controlled within a range; a validator or a searcher colluding with a validator can target a specific timestamp. Market maturities are set at creation and are publicly known, making them targetable. The offer setup via `setIsRootRatified` is fully permissionless. The attack is repeatable across any market whose maturity aligns with a block timestamp.

## Recommendation
Change `<=` to `<` at line 391:

```solidity
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

This ensures that at `block.timestamp == maturity`, the left operand is `false`, forcing `sellerDebtIncrease == 0` to be checked, consistent with the invariant at `live_context.json:221`.

## Proof of Concept
```solidity
// In a Foundry test:
// 1. Create a market with maturity = block.timestamp + 1 days
// 2. Create a sell offer with expiry = maturity, no existing credit
// 3. vm.warp(maturity)  // warp to exactly maturity
// 4. Call take() with units > 0
// 5. Assert sellerPos.debt == units  // debt was created at maturity
// 6. Assert block.timestamp == market.maturity  // confirms == boundary
// Expected: take() should revert with CannotIncreaseDebtPostMaturity
// Actual: take() succeeds and debt is written
``` [1](#0-0) [5](#0-4) [6](#0-5) [4](#0-3)

### Citations

**File:** src/Midnight.sol (L359-359)
```text
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
```

**File:** src/Midnight.sol (L384-384)
```text
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
```

**File:** src/Midnight.sol (L391-391)
```text
        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

**File:** src/Midnight.sol (L414-417)
```text
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
