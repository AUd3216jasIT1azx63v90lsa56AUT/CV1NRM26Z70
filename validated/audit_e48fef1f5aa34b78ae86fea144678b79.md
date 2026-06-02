Audit Report

## Title
Unconditional zero-amount `safeTransfer` in `liquidate()` permanently blocks bad-debt-only liquidations for zero-revert collateral tokens - (File: src/Midnight.sol)

## Summary
`liquidate()` unconditionally calls `SafeTransferLib.safeTransfer(collateralToken, receiver, seizedAssets)` at line 696 and `SafeTransferLib.safeTransferFrom(loanToken, payer, address(this), repaidUnits)` at line 717, even when both `seizedAssets` and `repaidUnits` are zero. This directly contradicts the NatSpec at line 577 which guarantees "Passing both 0 for seizedAssets and repaidUnits allows to realize bad debt with 0 token transferred." For any market whose collateral or loan token reverts on zero-amount transfers, the bad-debt-only liquidation path is permanently DoS'd, preventing `lossFactor` updates and lender loss socialization.

## Finding Description

**Verified code path:**

The NatSpec at line 577 explicitly documents the zero-zero path as a supported operation: [1](#0-0) 

The guard at line 595 uses `atMostOneNonZero`, which evaluates `or(iszero(x), iszero(y))`. When both inputs are zero, this returns `or(1,1) = 1 = true`, so the require passes: [2](#0-1) [3](#0-2) 

The block at line 643 is skipped entirely when both inputs are zero, leaving `seizedAssets = 0` and `repaidUnits = 0`: [4](#0-3) 

Bad-debt accounting at lines 626–641 executes correctly and modifies `_position.debt` and `_marketState`: [5](#0-4) 

Execution then falls through unconditionally to both transfer calls: [6](#0-5) [7](#0-6) 

`SafeTransferLib.safeTransfer` has no zero-amount guard — it always issues the low-level `token.call`: [8](#0-7) 

For a collateral token that reverts on zero-amount `transfer()`, the entire transaction reverts, rolling back all state changes including the bad-debt accounting. The same applies symmetrically to the loan token at line 717.

**Root cause:** Missing `if (seizedAssets > 0)` guard before line 696 and missing `if (repaidUnits > 0)` guard before line 717.

## Impact Explanation

Bad-debt-only liquidations (`seizedAssets = 0, repaidUnits = 0`) are permanently blocked for any market whose collateral or loan token reverts on zero-amount transfers. Consequences:

- `_marketState.lossFactor` is never updated — lenders cannot recover proportional losses
- `_position.debt` is never reduced — the borrower's bad debt persists indefinitely
- The core invariant "bad debt must reduce lender credit exactly once and proportionally" is violated
- The protocol's own documented guarantee at line 577 is broken

Market configuration is immutable post-deployment, so the collateral token cannot be replaced. This constitutes a permanent freeze of bad-debt realization and credit/debt accounting corruption, matching the "permanent or long-term fund freeze," "bad debt creation," and "credit/debt accounting corruption" impact classes in `live_context.json`. [9](#0-8) 

## Likelihood Explanation

**Preconditions:**
1. A market exists with a collateral token that reverts on zero-amount `transfer()`. Several deployed ERC20 tokens exhibit this behavior. The market creator role is explicitly listed as an unprivileged attacker in scope.
2. A borrower's position reaches `badDebt > 0` (collateral value insufficient to cover debt at `maxLif`), which is a normal market outcome.

Non-standard token behavior is explicitly in scope under `external_calls` and `recommended_fuzz_axes`, and the highest-priority question "Can non-standard token behavior break accounting assumptions?" directly covers this scenario: [10](#0-9) [11](#0-10) 

Once such a market exists and a position reaches bad-debt state, the DoS is triggered by any liquidator on every attempt. It is repeatable and permanent — market configuration is immutable post-deployment.

## Recommendation

Add zero-amount guards before both unconditional transfer calls:

```solidity
// Line 696 area:
if (seizedAssets > 0) {
    SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
}

// Line 717 area:
if (repaidUnits > 0) {
    SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
}
```

This aligns the implementation with the NatSpec guarantee at line 577 and ensures the bad-debt-only path executes without any token interaction.

## Proof of Concept

1. Deploy a collateral token that reverts on `transfer(to, 0)`.
2. Create a market using this token as collateral.
3. Have a borrower supply collateral and take debt.
4. Manipulate the oracle price so the position's collateral value falls below `debt / maxLif` (i.e., `badDebt > 0`).
5. Call `liquidate(market, collateralIndex, 0, 0, borrower, false, receiver, address(0), "")`.
6. Observe the transaction reverts at line 696 due to the zero-amount transfer to the zero-revert token.
7. Confirm `_marketState.lossFactor` and `_position.debt` are unchanged despite the position being in bad-debt state.
8. Repeat step 5 — every attempt reverts permanently, as the market config (and thus the collateral token) is immutable.

### Citations

**File:** src/Midnight.sol (L577-577)
```text
    /// @dev Passing both 0 for seizedAssets and repaidUnits allows to realize bad debt with 0 token transferred.
```

**File:** src/Midnight.sol (L595-595)
```text
        require(UtilsLib.atMostOneNonZero(repaidUnits, seizedAssets), InconsistentInput());
```

**File:** src/Midnight.sol (L626-641)
```text
        if (badDebt > 0) {
            // forge-lint: disable-next-item(unsafe-typecast) as badDebt <= _position.debt
            _position.debt -= uint128(badDebt);
            uint256 _totalUnits = _marketState.totalUnits;
            uint256 _lossFactor = _marketState.lossFactor;
            _marketState.lossFactor = UtilsLib.toUint128(
                type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
            );
            _marketState.totalUnits -= UtilsLib.toUint128(badDebt);
            _marketState.continuousFeeCredit = _lossFactor < type(uint128).max
                ? UtilsLib.toUint128(
                    _marketState.continuousFeeCredit
                        .mulDivDown(type(uint128).max - _marketState.lossFactor, type(uint128).max - _lossFactor)
                )
                : 0;
        }
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

**File:** src/libraries/UtilsLib.sol (L9-13)
```text
    function atMostOneNonZero(uint256 x, uint256 y) internal pure returns (bool z) {
        assembly {
            z := or(iszero(x), iszero(y))
        }
    }
```

**File:** src/libraries/SafeTransferLib.sol (L12-22)
```text
    function safeTransfer(address token, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());

        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transfer, (to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
        }
        require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferReturnedFalse());
    }
```

**File:** live_context.json (L53-66)
```json
    "best_bug_classes": [
      "direct loss of user funds",
      "protocol insolvency",
      "bad debt creation",
      "unauthorized collateral withdrawal",
      "unauthorized collateral seizure",
      "permanent or long-term fund freeze",
      "liquidation bypass",
      "healthy-account liquidation",
      "offer replay or overfill",
      "gate or ratifier bypass",
      "credit/debt accounting corruption",
      "callback or multicall state corruption"
    ]
```

**File:** live_context.json (L230-235)
```json
    "external_calls": [
      "callbacks must not corrupt partial state through reentrancy",
      "ERC20 transfer deltas must match accounting deltas",
      "fee-on-transfer, rebasing, false-return, ERC777-like hooks, and non-standard decimals should be tested if not explicitly excluded",
      "multicall must not bypass per-action invariants"
    ]
```

**File:** live_context.json (L385-394)
```json
    "external_behavior": [
      "callback reverts",
      "callback reenters",
      "token returns false",
      "token charges fee",
      "token rebases",
      "token has 6/8/18/27 decimals",
      "receiver is contract",
      "payer is different from msg.sender"
    ]
```
