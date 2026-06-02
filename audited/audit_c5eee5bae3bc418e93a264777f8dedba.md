Audit Report

## Title
Unconditional `safeTransfer(collateral, receiver, 0)` in `liquidate` blocks bad debt realization for tokens that revert on zero-value transfers - (File: `src/Midnight.sol`)

## Summary
The `liquidate` function calls `SafeTransferLib.safeTransfer` at line 696 unconditionally, even when `seizedAssets=0` and `repaidUnits=0` (the bad debt realization path). If the collateral token reverts on zero-value transfers, the entire transaction reverts, rolling back all bad debt state mutations already written to storage at lines 626–641. This directly contradicts the protocol's own NatSpec at line 577, which guarantees that passing both zeros realizes bad debt with no token transferred.

## Finding Description

**Exact code path:**

1. **Lines 626–641** (`src/Midnight.sol`): When `badDebt > 0`, the function mutates `_position.debt`, `_marketState.lossFactor`, `_marketState.totalUnits`, and `_marketState.continuousFeeCredit` in storage. [1](#0-0) 

2. **Lines 643–677**: The collateral seizure and repayment block is correctly guarded by `if (repaidUnits > 0 || seizedAssets > 0)` and is skipped when both are zero. [2](#0-1) 

3. **Line 696**: `SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets)` is called **unconditionally**, outside any guard, with `seizedAssets` still equal to `0`. [3](#0-2) 

4. **Line 717**: `SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits)` is also called unconditionally with `repaidUnits=0`, creating a symmetric issue on the loan token side. [4](#0-3) 

**Root cause in `SafeTransferLib.safeTransfer`:**

The library makes an unconditional low-level `call` to `token.transfer(to, value)`. If the call returns `success=false`, the assembly block re-reverts with the token's error data. If the token returns `false`, it reverts with `TransferReturnedFalse`. Either path propagates the revert through `liquidate`, rolling back all storage writes. [5](#0-4) 

**Why existing checks do not stop it:**

The `atMostOneNonZero` check at line 595 only validates that at most one of `repaidUnits`/`seizedAssets` is nonzero — it explicitly permits both being zero (the bad debt path). The `_position.debt > 0` check at line 596 is satisfied by the bad debt precondition. No guard exists on the `safeTransfer` call itself. [6](#0-5) 

**Formal verification gap:**

The Certora spec rule `liquidateLossFactorDoesNotRevert` (lines 89–106 of `certora/specs/LossFactor.spec`) explicitly tests `liquidate(0, 0, 0, borrower, ...)` and asserts it must not revert — but it stubs out both `SafeTransferLib.safeTransfer` and `SafeTransferLib.safeTransferFrom` as `NONDET` at lines 21–22. The proof holds only under the assumption that transfers never revert; real-world token behavior is not covered. [7](#0-6) [8](#0-7) 

## Impact Explanation

When the collateral token reverts on zero-value transfers, every call to `liquidate(0, 0)` reverts. Because the bad debt state writes occur before the `safeTransfer` at line 696, they are all rolled back on revert:

- `_marketState.lossFactor` is never updated → lenders' credit is never slashed proportionally
- `_marketState.totalUnits` is never reduced → the accounting invariant `totalUnits == sum(debts)` is permanently broken
- The borrower's `_position.debt` is never reduced → the position remains in a permanently bad-debt state
- The market becomes permanently insolvent: no liquidation path can succeed for this borrower

This constitutes a permanent freeze of protocol accounting state and unrecoverable corruption of the loss-factor mechanism, which is a critical integrity failure.

## Likelihood Explanation

**Preconditions:**
1. A market must exist whose collateral token reverts on zero-value transfers. This is a known behavior of several deployed ERC20 tokens (e.g., tokens that enforce `amount > 0` in their `transfer` implementation). In a permissionless protocol, any market creator can deploy such a market, intentionally or not.
2. A borrower in that market must have `collateralValue < debt` (bad debt state), which is a normal market condition reachable through price movement.

Both preconditions are independently reachable without any privileged action. The attack is repeatable: every subsequent `liquidate(0, 0)` call will also revert, making the block permanent. No special role is required — any unprivileged liquidator can trigger this.

## Recommendation

Guard both transfer calls with a nonzero check, consistent with the existing guard at line 643:

```solidity
// Line 696 — guard the collateral transfer
if (seizedAssets > 0) {
    SafeTransferLib.safeTransfer(
        market.collateralParams[collateralIndex].token, receiver, seizedAssets
    );
}

// Line 717 — guard the loan token transfer
if (repaidUnits > 0) {
    SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
}
```

This aligns the transfer calls with the NatSpec guarantee at line 577 and with the existing `if (repaidUnits > 0 || seizedAssets > 0)` guard pattern already used in the function body. [9](#0-8) 

## Proof of Concept

**Minimal Foundry test plan:**

1. Deploy a mock ERC20 collateral token whose `transfer` reverts unconditionally when `value == 0`.
2. Create a Midnight market using this token as collateral.
3. Open a borrow position and manipulate the oracle price so that `collateralValue < debt` (bad debt state).
4. Call `liquidate(market, collateralIndex, 0, 0, borrower, false, receiver, address(0), "")`.
5. **Expected (per NatSpec):** Transaction succeeds; `_marketState.lossFactor` and `_marketState.totalUnits` are updated.
6. **Actual:** Transaction reverts at line 696 due to the token's zero-value transfer revert; all storage writes are rolled back.
7. Confirm by asserting `marketState[id].lossFactor` and `position[id][borrower].debt` are unchanged after the call.

### Citations

**File:** src/Midnight.sol (L575-577)
```text
    /// @dev See LIQUIDATIONS section for more details.
    /// @dev At least one of seizedAssets or repaidUnits should be equal to zero.
    /// @dev Passing both 0 for seizedAssets and repaidUnits allows to realize bad debt with 0 token transferred.
```

**File:** src/Midnight.sol (L595-596)
```text
        require(UtilsLib.atMostOneNonZero(repaidUnits, seizedAssets), InconsistentInput());
        require(_position.debt > 0, NotBorrower()); // to avoid no-op liquidations of non borrower positions.
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

**File:** certora/specs/LossFactor.spec (L20-22)
```text
    // SafeTransferLib summaries: bypass transfer logic (needed for liquidate @withrevert rules).
    function SafeTransferLib.safeTransfer(address, address, uint256) internal => NONDET;
    function SafeTransferLib.safeTransferFrom(address, address, address, uint256) internal => NONDET;
```

**File:** certora/specs/LossFactor.spec (L89-106)
```text
/// The loss factor arithmetic in liquidate does not revert under valid state. Uses seizedAssets=0, repaidUnits=0 to isolate the bad debt realization path. Uses collateralBitmap=0 to skip the collateral loop, ensuring badDebt == position.debt.
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
