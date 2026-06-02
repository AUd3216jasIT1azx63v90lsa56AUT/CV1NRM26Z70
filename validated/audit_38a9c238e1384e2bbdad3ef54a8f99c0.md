All key claims check out against the actual code. Let me verify the `atMostOneNonZero` behavior for the both-zero case and confirm the unconditional transfer path.

Audit Report

## Title
Missing zero-amount guard before `safeTransfer`/`safeTransferFrom` in `liquidate()` blocks bad-debt-only liquidations for tokens that revert on zero transfers - (`File: src/Midnight.sol`)

## Summary
`liquidate()` unconditionally calls `SafeTransferLib.safeTransfer` at line 696 and `SafeTransferLib.safeTransferFrom` at line 717 even when `seizedAssets = 0` and `repaidUnits = 0` respectively. The NatSpec at line 577 explicitly guarantees the zero-zero path as valid for bad-debt-only liquidations. For any market whose collateral or loan token reverts on zero-amount transfers, the bad-debt realization path is permanently DoS'd, preventing `lossFactor` from ever being updated and leaving bad debt unrealized.

## Finding Description

**Step 1 — NatSpec guarantee.** The function's NatSpec at line 577 explicitly documents the zero-zero path:
> "Passing both 0 for seizedAssets and repaidUnits allows to realize bad debt with 0 token transferred." [1](#0-0) 

**Step 2 — Guard passes for both-zero inputs.** The guard at line 595 calls `UtilsLib.atMostOneNonZero(repaidUnits, seizedAssets)`, which is implemented as `or(iszero(x), iszero(y))`. When both inputs are zero, `iszero(0) = 1`, so `or(1, 1) = 1` (true). The require passes. [2](#0-1) [3](#0-2) 

**Step 3 — Collateral/repayment block is skipped.** The block at line 643 is gated on `repaidUnits > 0 || seizedAssets > 0`. When both are zero, this block is skipped entirely, leaving `seizedAssets = 0` and `repaidUnits = 0`. [4](#0-3) 

**Step 4 — Bad-debt accounting executes correctly.** The `if (badDebt > 0)` block at lines 626–641 runs and correctly updates `_position.debt`, `_marketState.lossFactor`, `_marketState.totalUnits`, and `_marketState.continuousFeeCredit`. [5](#0-4) 

**Step 5 — Unconditional `safeTransfer` with zero amount.** Execution falls through to line 696 unconditionally, calling `SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, 0)`. [6](#0-5) 

**Step 6 — `SafeTransferLib` has no zero-amount guard.** `safeTransfer` always issues `token.call(abi.encodeCall(IERC20.transfer, (to, value)))` regardless of `value`. If the collateral token reverts on a zero-amount `transfer()`, the entire transaction reverts, rolling back all state changes from steps 4. [7](#0-6) 

**Step 7 — Same issue for `safeTransferFrom` at line 717.** When `repaidUnits = 0`, `SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), 0)` is also called unconditionally. If the loan token reverts on zero-amount `transferFrom()`, the same DoS applies. [8](#0-7) 

**Step 8 — Certora verification misses this.** The `liquidateLossFactorDoesNotRevert` rule in `LossFactor.spec` (line 104) tests the zero-zero path but summarizes both `SafeTransferLib.safeTransfer` and `SafeTransferLib.safeTransferFrom` as `NONDET` (lines 21–22), abstracting away the external ERC20 call entirely. The formal verification therefore cannot detect a revert originating from the token. [9](#0-8) [10](#0-9) 

## Impact Explanation

Bad-debt-only liquidations are permanently blocked for any market whose collateral token (or loan token) reverts on zero-amount transfers. The `_marketState.lossFactor` is never updated, lenders cannot recover proportional losses through the loss socialization mechanism, and the borrower's debt is never reduced. This directly violates the protocol's own documented guarantee and the core accounting invariant. The impact is a permanent freeze of the bad-debt realization path — a concrete, non-hypothetical loss of protocol functionality.

## Likelihood Explanation

The market creator role is unprivileged; any user can create a market with any ERC20 as collateral or loan token. Tokens that revert on zero-amount transfers are a known real-world pattern present in several deployed tokens. Once such a market exists and any borrower's position reaches `badDebt > 0`, every bad-debt-only liquidation attempt reverts. The DoS is repeatable, permanent (the collateral token cannot be changed post-deployment), and requires no special timing, oracle manipulation, or victim mistakes.

## Recommendation

Add a zero-amount guard before each unconditional transfer in `liquidate()`. Specifically:

1. Wrap the `safeTransfer` at line 696 in a condition: only call it when `seizedAssets > 0`.
2. Wrap the `safeTransferFrom` at line 717 in a condition: only call it when `repaidUnits > 0`.

This matches the existing pattern used elsewhere in the codebase (e.g., `MidnightBundles.sol` line 167: `if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(...)`) and is consistent with the NatSpec guarantee that the zero-zero path transfers no tokens.

## Proof of Concept

1. Deploy a collateral ERC20 token that reverts on `transfer(to, 0)`.
2. Create a Midnight market using this token as collateral.
3. Have a borrower supply collateral and borrow such that the position eventually reaches `badDebt > 0` (e.g., collateral value drops to zero while debt remains).
4. Call `liquidate(market, collateralIndex, 0, 0, borrower, false, receiver, address(0), "")`.
5. Observe: the call reverts at line 696 because `safeTransfer(collateralToken, receiver, 0)` triggers the token's zero-transfer revert.
6. Confirm: `_marketState.lossFactor` is unchanged, `_position.debt` is unchanged — bad debt is permanently unrealized.

A minimal Foundry test can implement a mock ERC20 that reverts on `transfer(_, 0)`, set up the above state, and assert that `liquidate` reverts with the token's revert reason while `marketState.lossFactor` remains at its pre-call value.

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

**File:** src/libraries/UtilsLib.sol (L9-12)
```text
    function atMostOneNonZero(uint256 x, uint256 y) internal pure returns (bool z) {
        assembly {
            z := or(iszero(x), iszero(y))
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

**File:** certora/specs/LossFactor.spec (L21-22)
```text
    function SafeTransferLib.safeTransfer(address, address, uint256) internal => NONDET;
    function SafeTransferLib.safeTransferFrom(address, address, address, uint256) internal => NONDET;
```

**File:** certora/specs/LossFactor.spec (L90-106)
```text
rule liquidateLossFactorDoesNotRevert(env e, Midnight.Market market, address borrower, bytes data) {
    bytes32 id = summaryToId(market);

    require data.length == 0, "no callback to avoid unrelated external call reverts";
    require marketIsCreated(market), "market must be created";
    require market.liquidatorGate == 0, "Assumption:no liquidator gate";
    require market.collateralParams.length > 0, "market has at least one collateral (enforced by touchMarket)";
    require !liquidationLocked(id, borrower), "liquidation not locked (transient storage is zero at transaction start)";
    require currentContract.position[id][borrower].collateralBitmap == 0, "Assumption: no active collaterals: skip loop and maximize badDebt";
    require currentContract.position[id][borrower].debt > 0, "borrower must have debt to enter badDebt > 0 block";
    require currentContract.position[id][borrower].debt <= currentContract.marketState[id].totalUnits, "position debt bounded by totalUnits (see totalUnitsEqualsSumNegativeDebtPlusWithdrawable)";
    require e.msg.value == 0, "Midnight is not payable";

    address zero = 0;
    liquidate@withrevert(e, market, 0, 0, 0, borrower, false, borrower, zero, data);

    assert !lastReverted, "liquidate should not revert under valid state (bad debt realization path)";
```
