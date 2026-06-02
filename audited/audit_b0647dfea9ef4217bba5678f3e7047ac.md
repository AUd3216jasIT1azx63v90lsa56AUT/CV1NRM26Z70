Audit Report

## Title
Debt Increase Permitted at Exact Maturity Timestamp via `<=` Guard in `take` — (`src/Midnight.sol`)

## Summary
The maturity guard in `Midnight.take` at line 391 uses `block.timestamp <= offer.market.maturity`, which evaluates to `true` when `block.timestamp == offer.market.maturity`. This allows `sellerDebtIncrease > 0` to be written to storage at the exact maturity second, directly violating the protocol's explicitly documented invariant that "maturity boundary must not allow debt increase by rounding, **timestamp equality**, or callback sequencing" (`live_context.json` line 221) and "debt must not increase after maturity" (`live_context.json` line 197).

## Finding Description

**Root cause:** The guard at `src/Midnight.sol` line 391 is:

```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
``` [1](#0-0) 

When `block.timestamp == offer.market.maturity`, the left operand is `true`, so the `require` passes unconditionally regardless of `sellerDebtIncrease`.

**Debt computation:** `sellerDebtIncrease` is computed at line 384 as `units - sellerCreditDecrease`. If the seller has no existing credit (`sellerPos.credit == 0`), then `sellerCreditDecrease == 0` and `sellerDebtIncrease == units > 0`. [2](#0-1) 

**Debt write:** The increase is committed to storage at line 414:

```solidity
sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
``` [3](#0-2) 

**Multicall amplification:** `multicall` at lines 211–220 iterates `delegatecall` over an arbitrary array of encoded calls. All iterations share the same `block.timestamp`, so an attacker can batch multiple `take` calls against different offers in a single transaction, each independently passing the `<=` guard and each writing a separate `sellerDebtIncrease` to the same or different seller positions. [4](#0-3) 

**Why existing checks fail:**
- The `CannotIncreaseDebtPostMaturity` guard itself is the defective check — `<=` instead of `<`.
- The `enterGate.canIncreaseDebt` check (lines 402–406) is an optional market-level gate, not a maturity enforcement mechanism.
- The `SellerIsLiquidatable` check at line 476 only reverts if the seller is *already* liquidatable before the trade; it does not prevent new debt creation at maturity.
- `timeToMaturity` at line 359 uses `zeroFloorSub(offer.market.maturity, block.timestamp)`, which equals `0` at `block.timestamp == maturity`, so `buyerPendingFeeIncrease` is zero — but this does not block the debt write. [5](#0-4) [6](#0-5) 

**Violated invariants (live_context.json):** [7](#0-6) [8](#0-7) 

## Impact Explanation
A borrower receives loan assets in exchange for debt that is immediately overdue at the moment of creation. Per the protocol's own liquidation invariant ("overdue debt after maturity is liquidatable"), this debt is immediately subject to liquidation, meaning the borrower has taken on a position that cannot be repaid in time. The lender's credit is backed by debt that should never have existed, violating solvency accounting. Via `multicall`, this can be repeated across multiple offers atomically, amplifying bad debt in a single transaction.

## Likelihood Explanation
The only precondition is `block.timestamp == offer.market.maturity`, a single deterministic second that is publicly known from market creation. Any participant can monitor the chain and submit the transaction in the target block. Block proposers/builders can guarantee inclusion. No privileged access, oracle manipulation, or victim error is required. The attack is repeatable across every market at its maturity second.

## Recommendation
Change the guard from `<=` to `<`:

```solidity
// Before (vulnerable):
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());

// After (correct):
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

This ensures that at `block.timestamp == maturity`, the left operand is `false`, so the `require` only passes if `sellerDebtIncrease == 0`, matching the invariant's explicit prohibition of timestamp-equality debt increases.

## Proof of Concept

```solidity
// Minimal Foundry test sketch
function test_debtIncreaseAtExactMaturity() public {
    // 1. Create a market with maturity = T
    // 2. Create a sell-side offer from maker (seller) with no existing credit
    // 3. vm.warp(market.maturity);  // block.timestamp == maturity exactly
    // 4. Call take(offer, ..., units > 0, ...)
    // 5. Assert sellerPos.debt > 0  // debt was written
    // 6. Assert block.timestamp == market.maturity  // at exact boundary
    // Expected: step 4 should revert with CannotIncreaseDebtPostMaturity
    // Actual:   step 4 succeeds and step 5 passes — BUG CONFIRMED
}
```

For the `multicall` amplification path, encode two `take` calls against different offers and pass them to `multicall` in step 4; both will write `sellerDebtIncrease` in the same block.

### Citations

**File:** src/Midnight.sol (L211-220)
```text
    function multicall(bytes[] calldata calls) external {
        for (uint256 i = 0; i < calls.length; i++) {
            (bool success, bytes memory returnData) = address(this).delegatecall(calls[i]);
            if (!success) {
                assembly ("memory-safe") {
                    revert(add(returnData, 0x20), mload(returnData))
                }
            }
        }
    }
```

**File:** src/Midnight.sol (L359-359)
```text
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
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

**File:** src/Midnight.sol (L476-476)
```text
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

**File:** live_context.json (L197-197)
```json
      "debt must not increase after maturity"
```

**File:** live_context.json (L219-222)
```json
    "maturity": [
      "pre-maturity and post-maturity paths must not be composable to bypass debt repayment",
      "maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing",
      "post-maturity unwinding must not mint free credit or erase debt incorrectly"
```
