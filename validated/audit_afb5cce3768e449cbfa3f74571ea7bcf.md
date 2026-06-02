The code confirms the claim exactly. Both transfer calls at lines 696 and 717 are unconditional, and the NatSpec at line 577 explicitly documents the zero-zero path as valid.

Audit Report

## Title
Unconditional `safeTransfer` and `safeTransferFrom` with zero amounts in `liquidate()` block bad-debt-only liquidations on zero-revert tokens - (File: src/Midnight.sol)

## Summary
`liquidate()` unconditionally calls `SafeTransferLib.safeTransfer` (line 696) and `SafeTransferLib.safeTransferFrom` (line 717) even when `seizedAssets` and `repaidUnits` are both 0. The NatSpec at line 577 explicitly documents this zero-zero path as valid for bad-debt realization. For any collateral or loan token that reverts on zero-amount transfers, every bad-debt realization attempt permanently reverts, freezing `lossFactor` and `totalUnits` accounting.

## Finding Description
The guard at line 643 correctly skips collateral/debt accounting when both inputs are zero:

```solidity
if (repaidUnits > 0 || seizedAssets > 0) { ... }
```

After this block, `seizedAssets` and `repaidUnits` remain 0. The bad-debt accounting block at lines 626–641 executes correctly, updating `_position.debt`, `_marketState.lossFactor`, `_marketState.totalUnits`, and `_marketState.continuousFeeCredit`. Then, unconditionally:

- Line 696: `SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets)` — called with `seizedAssets = 0`
- Line 717: `SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits)` — called with `repaidUnits = 0`

`SafeTransferLib.safeTransfer` (lines 15–19 of `src/libraries/SafeTransferLib.sol`) makes a raw low-level call and propagates any revert from the token. If the collateral token reverts on `transfer(receiver, 0)`, the entire transaction reverts — including all bad-debt accounting already performed in the same call.

**Why existing checks fail:**
- `atMostOneNonZero(repaidUnits, seizedAssets)` at line 595 explicitly permits both to be 0 — this is the intended bad-debt path.
- `require(_position.debt > 0, NotBorrower())` at line 596 is satisfied by any borrower with outstanding debt.
- There is no `if (seizedAssets > 0)` guard before the `safeTransfer` call at line 696.
- There is no `if (repaidUnits > 0)` guard before the `safeTransferFrom` call at line 717.

The NatSpec at line 577 directly contradicts the implementation: `"Passing both 0 for seizedAssets and repaidUnits allows to realize bad debt with 0 token transferred."` The implementation always issues both transfer calls regardless. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

## Impact Explanation
Bad-debt-only liquidations are permanently and repeatably blocked for any market whose collateral token or loan token reverts on zero-amount transfers. The `lossFactor` and `totalUnits` state that socializes bad debt among lenders cannot be updated. Unhealthy positions with full bad debt remain stuck indefinitely. Lenders in affected markets cannot recover their proportional share of losses through the documented bad-debt mechanism. This is a concrete, permanent DoS on a core protocol invariant explicitly documented in the NatSpec.

## Likelihood Explanation
**Preconditions:**
1. A market is created with a collateral token that reverts on `transfer(to, 0)` (e.g., BNB, LEND, and various custom tokens). Market creation is permissionless in Midnight, so any user can deploy such a market.
2. A borrower's position reaches a state where `badDebt > 0` — a normal market condition under adverse price movements.
3. Any unprivileged liquidator calls `liquidate(market, idx, 0, 0, borrower, ...)`.

No special privilege is required. The attack is repeatable on every bad-debt realization attempt.

## Recommendation
Add zero-amount guards before both unconditional transfer calls:

```solidity
// Line 696 — guard before collateral transfer
if (seizedAssets > 0) {
    SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
}

// Line 717 — guard before loan token transfer
if (repaidUnits > 0) {
    SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
}
```

This aligns the implementation with the NatSpec guarantee at line 577 and ensures bad-debt-only liquidations (both inputs zero) never attempt token transfers.

## Proof of Concept
1. Deploy a mock ERC-20 collateral token that reverts on `transfer(to, 0)`.
2. Create a Midnight market using this token as collateral.
3. Open a borrow position and drive the collateral price to zero so `badDebt == originalDebt` and `seizedAssets == 0`.
4. Call `liquidate(market, collateralIndex, 0, 0, borrower, false, receiver, address(0), "")`.
5. Observe the transaction reverts at line 696 despite the bad-debt accounting at lines 626–641 having executed correctly.
6. Confirm `_marketState.lossFactor` and `_marketState.totalUnits` are unchanged (state rolled back), and the position remains stuck.

### Citations

**File:** src/Midnight.sol (L577-577)
```text
    /// @dev Passing both 0 for seizedAssets and repaidUnits allows to realize bad debt with 0 token transferred.
```

**File:** src/Midnight.sol (L643-643)
```text
        if (repaidUnits > 0 || seizedAssets > 0) {
```

**File:** src/Midnight.sol (L696-696)
```text
        SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
```

**File:** src/Midnight.sol (L717-717)
```text
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
```

**File:** src/libraries/SafeTransferLib.sol (L15-19)
```text
        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transfer, (to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
```
