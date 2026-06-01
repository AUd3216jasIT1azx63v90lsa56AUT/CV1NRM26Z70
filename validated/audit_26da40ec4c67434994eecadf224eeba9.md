Audit Report

## Title
Unconditional zero-value `safeTransfer` on bad-debt-only liquidation path blocks bad debt realization for revert-on-zero collateral tokens - (File: src/Midnight.sol)

## Summary
When `liquidate` is called with `seizedAssets=0` and `repaidUnits=0` (the explicitly documented bad-debt-only path), the bad debt accounting at lines 626–641 mutates state correctly, but `SafeTransferLib.safeTransfer(collateralToken, receiver, 0)` at line 696 is executed unconditionally. If the collateral token reverts on zero-value `transfer` calls, the entire transaction reverts, undoing the already-applied `lossFactor` and `totalUnits` updates and permanently blocking bad debt socialization for that market.

## Finding Description
**NatSpec contract (lines 576–579):** The function explicitly documents that passing both `seizedAssets=0` and `repaidUnits=0` "allows to realize bad debt with 0 token transferred." [1](#0-0) 

**Gate at line 595:** `atMostOneNonZero(repaidUnits, seizedAssets)` passes when both are zero, so the call proceeds. [2](#0-1) 

**State mutation at lines 626–641:** `_position.debt`, `_marketState.lossFactor`, `_marketState.totalUnits`, and `_marketState.continuousFeeCredit` are all updated before any transfer occurs. [3](#0-2) 

**Accounting gate at line 643:** `if (repaidUnits > 0 || seizedAssets > 0)` is `false` when both are zero, so `seizedAssets` remains `0` after this block. [4](#0-3) 

**Unconditional transfer at line 696:** `SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets)` is called with `seizedAssets == 0`, with no guard. [5](#0-4) 

**`safeTransfer` behavior:** The library makes an unconditional external `token.call(transfer(receiver, 0))`. If the token returns `success = false` or reverts, the library propagates the revert via inline assembly. [6](#0-5) 

The revert unwinds all state changes from lines 626–641, leaving bad debt unaccounted.

## Impact Explanation
Bad debt cannot be socialized for any market whose collateral token reverts on zero-value `transfer` calls. The `lossFactor` and `totalUnits` updates that distribute losses among lenders are permanently blocked. Lenders cannot redeem at the correct adjusted loss factor, and the protocol's accounting invariant — that every unit of debt is either active, settled, or socialized as loss — is violated. This constitutes a permanent freeze of the bad-debt realization mechanism for affected markets.

## Likelihood Explanation
Tokens that revert on zero-value transfers exist in production (e.g., LEND token, certain rebasing and vault tokens). Market creation is permissionless, so any user can deploy a market with such a collateral token. The bad-debt-only liquidation path (`liquidate(0,0)`) is explicitly documented and expected to be called. The failure condition is deterministic and repeatable: every attempt to realize bad debt in such a market will revert.

## Recommendation
Guard the `safeTransfer` call at line 696 with a check on `seizedAssets`:

```solidity
if (seizedAssets > 0) {
    SafeTransferLib.safeTransfer(
        market.collateralParams[collateralIndex].token,
        receiver,
        seizedAssets
    );
}
```

This aligns the implementation with the documented invariant and eliminates the unnecessary external call on the bad-debt-only path.

## Proof of Concept
1. Deploy a mock ERC-20 collateral token that reverts on `transfer(to, 0)`.
2. Create a market using that token as collateral (permissionless).
3. Open a borrower position that becomes undercollateralized (bad debt: `badDebt > 0`, `maxDebt < debt`).
4. Call `liquidate(market, collateralIndex, 0, 0, borrower, false, receiver, address(0), "")`.
5. Observe: the call reverts despite the position being liquidatable, and `_marketState.lossFactor` / `_marketState.totalUnits` are unchanged.
6. Confirm: wrapping the `safeTransfer` in `if (seizedAssets > 0)` makes the same call succeed and correctly updates `lossFactor` and `totalUnits`.

### Citations

**File:** src/Midnight.sol (L576-579)
```text
    /// @dev At least one of seizedAssets or repaidUnits should be equal to zero.
    /// @dev Passing both 0 for seizedAssets and repaidUnits allows to realize bad debt with 0 token transferred.
    /// @dev Liquidations with both 0 for seizedAssets and repaidUnits can be done with a collateral that is not
    /// activated.
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

**File:** src/libraries/SafeTransferLib.sol (L12-21)
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
```
