Audit Report

## Title
Unconditional zero-value `safeTransfer` on bad-debt-only liquidation path blocks bad debt realization for revert-on-zero collateral tokens - (File: src/Midnight.sol)

## Summary
When `liquidate` is called with `seizedAssets=0` and `repaidUnits=0` (the explicitly documented bad-debt-only path), all bad-debt accounting state changes at lines 626–641 are correctly applied, but the unconditional `SafeTransferLib.safeTransfer(collateralToken, receiver, 0)` at line 696 will revert for any collateral token that rejects zero-value `transfer` calls, unwinding every state mutation and permanently blocking bad debt socialization for that market.

## Finding Description

**Gate at line 595 passes for `(0, 0)`:**
`atMostOneNonZero` uses `z := or(iszero(x), iszero(y))`. When both inputs are zero, `or(1, 1) = 1` (true), so the require passes. [1](#0-0) 

**NatSpec documents the `(0, 0)` path as intentional:**
Lines 577–579 explicitly state that passing both zero "allows to realize bad debt with 0 token transferred" and that this path "can be done with a collateral that is not activated." [2](#0-1) 

**State mutations occur before any transfer:**
When `badDebt > 0`, `_position.debt`, `_marketState.lossFactor`, `_marketState.totalUnits`, and `_marketState.continuousFeeCredit` are all mutated at lines 626–641. [3](#0-2) 

**`seizedAssets` remains 0 through line 696:**
The accounting block at line 643 is gated on `repaidUnits > 0 || seizedAssets > 0`, which is `false` for the `(0, 0)` call, so `seizedAssets` is never updated from its initial value of `0`. [4](#0-3) 

**Unconditional transfer at line 696:**
`SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets)` is called with no zero-value guard, passing `seizedAssets == 0` to the token. [5](#0-4) 

**`SafeTransferLib` propagates the revert:**
The library makes an unconditional external call `token.call(abi.encodeCall(IERC20.transfer, (to, value)))`. If `success == false`, it reverts via inline assembly, unwinding all prior state changes. [6](#0-5) 

**Same issue on line 717 for the loan token:**
`SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits)` is also called unconditionally with `repaidUnits == 0`. [7](#0-6) 

## Impact Explanation
Bad debt cannot be socialized for any market whose collateral token (or loan token) reverts on zero-value `transfer`/`transferFrom` calls. The `lossFactor` and `totalUnits` updates that distribute losses among lenders are permanently blocked. Lenders cannot redeem at the correct adjusted loss factor, and the protocol's accounting invariant — that every unit of debt is either active, settled, or socialized as loss — is violated for affected markets. This is a permanent, deterministic freeze of the bad-debt realization mechanism.

## Likelihood Explanation
Tokens that revert on zero-value transfers exist in production (e.g., LEND, certain rebasing and vault tokens). Market creation is permissionless, so any user can deploy a market with such a collateral token. The `(0, 0)` liquidation path is explicitly documented and expected to be called by liquidators. The failure is deterministic and repeatable: every attempt to realize bad debt in such a market will revert.

## Recommendation
Add zero-value guards before both transfer calls:

```solidity
// Line 696
if (seizedAssets > 0) {
    SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
}

// Line 717
if (repaidUnits > 0) {
    SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
}
```

This matches the documented intent ("realize bad debt with 0 token transferred") and is consistent with the existing `if (repaidUnits > 0 || seizedAssets > 0)` accounting gate at line 643.

## Proof of Concept
1. Deploy a collateral token that reverts on zero-value `transfer` calls.
2. Create a permissionless market using that token as collateral.
3. Open a borrower position that becomes insolvent with `badDebt > 0` (all collateral value is below the debt).
4. Call `liquidate(market, collateralIndex, 0, 0, borrower, false, receiver, address(0), "")`.
5. Observe: state mutations at lines 626–641 execute, then `safeTransfer(token, receiver, 0)` is called, the token reverts, the entire transaction reverts, and `lossFactor`/`totalUnits` are never updated.
6. Repeat indefinitely — bad debt can never be socialized for this market.

### Citations

**File:** src/libraries/UtilsLib.sol (L9-13)
```text
    function atMostOneNonZero(uint256 x, uint256 y) internal pure returns (bool z) {
        assembly {
            z := or(iszero(x), iszero(y))
        }
    }
```

**File:** src/Midnight.sol (L576-579)
```text
    /// @dev At least one of seizedAssets or repaidUnits should be equal to zero.
    /// @dev Passing both 0 for seizedAssets and repaidUnits allows to realize bad debt with 0 token transferred.
    /// @dev Liquidations with both 0 for seizedAssets and repaidUnits can be done with a collateral that is not
    /// activated.
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

**File:** src/libraries/SafeTransferLib.sol (L15-20)
```text
        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transfer, (to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
        }
```
