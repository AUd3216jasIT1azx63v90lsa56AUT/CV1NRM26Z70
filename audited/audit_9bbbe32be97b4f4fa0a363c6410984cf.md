Audit Report

## Title
Fee-on-Transfer Token Drains Protocol Balance via `flashLoan` Nominal-Amount Repayment - (File: src/Midnight.sol)

## Summary
The `flashLoan` function disburses `assets[i]` and then pulls back the same nominal `assets[i]`, with no balance snapshot or post-repayment balance assertion. For any fee-on-transfer token, the inbound leg delivers strictly less than `assets[i]` to the protocol, creating a net loss per call. Because market creation is permissionless, any caller can introduce a fee-on-transfer token and repeat the drain until the protocol's entire balance of that token is exhausted.

## Finding Description
**Exact code path — `src/Midnight.sol` lines 737–752:**

```solidity
function flashLoan(address[] calldata tokens, uint256[] calldata assets, address callback, bytes calldata data)
    external
{
    require(tokens.length == assets.length, InconsistentInput());          // line 740
    for (uint256 i = 0; i < tokens.length; i++) {
        SafeTransferLib.safeTransfer(tokens[i], callback, assets[i]);      // line 743
    }
    require(
        IFlashLoanCallback(callback).onFlashLoan(...) == CALLBACK_SUCCESS, // line 746
        WrongFlashLoanCallbackReturnValue()
    );
    for (uint256 i = 0; i < tokens.length; i++) {
        SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i]); // line 750
    }
}
```

**Root cause:** Both legs use the same nominal value `assets[i]`. `SafeTransferLib` only verifies the call did not revert and returned `true`; it does not verify the amount actually credited to `address(this)`. No `balanceOf(address(this))` snapshot is taken before the loan, and no assertion checks that the balance is restored afterward.

**Exploit flow:**
1. Attacker creates a market whose loan/collateral token is a fee-on-transfer token (permissionless via `touchMarket`), causing Midnight to accumulate a balance `B`.
2. Attacker calls `flashLoan([feeToken], [assets], attackerCallback, "")`.
3. Line 743: Midnight sends `assets`; callback receives `assets * (1 - r)`.
4. Attacker's `onFlashLoan` returns `CALLBACK_SUCCESS`.
5. Line 750: `safeTransferFrom` pulls `assets` from callback; Midnight receives `assets * (1 - r)` (single-sided fee) or `assets * (1 - r)^2` (double-sided fee). Net loss per call: at minimum `assets * r`.
6. Repeat (no cooldown, no rate limit) until balance is zero.

**Why existing checks fail:**
- The only guards are `tokens.length == assets.length` (line 740) and the callback return value (line 746) — neither touches token balances.
- `SafeTransferLib.safeTransferFrom` only checks call success, not credited amount.
- `certora/specs/Solvency.spec` line 31 explicitly states: *"Assume ERC20 tokens transfer correctly: no fee taking from sender or receiver, no rebasing"* — confirming the formal proofs provide zero on-chain protection against this token class.

## Impact Explanation
Midnight's balance of the fee-on-transfer token decreases by at least `assets * fee_rate` per `flashLoan` call. An attacker can loop this (including via `multicall`) until the full protocol balance is drained. Lenders and borrowers holding positions in that token can no longer be made whole, directly violating the core solvency invariant (`tokenBalanceCorrect`) that the contract balance must cover collateral, withdrawable, and claimable settlement fee amounts.

## Likelihood Explanation
Both preconditions are fully attacker-controlled with no privileged access:
- **Fee-on-transfer token balance in Midnight:** Achievable by any user via permissionless `touchMarket` followed by supply/collateral deposit.
- **Callback contract returning `CALLBACK_SUCCESS`:** Trivially deployable by any attacker.

Fee-on-transfer tokens are a well-known, deployed ERC20 variant on mainnet. The attack is repeatable with zero cooldown; each call independently drains `fee_rate * assets` tokens.

## Recommendation
Replace the nominal-amount repayment check with a balance-before/after assertion:

```solidity
uint256[] memory balancesBefore = new uint256[](tokens.length);
for (uint256 i = 0; i < tokens.length; i++) {
    balancesBefore[i] = IERC20(tokens[i]).balanceOf(address(this));
    SafeTransferLib.safeTransfer(tokens[i], callback, assets[i]);
}
require(IFlashLoanCallback(callback).onFlashLoan(...) == CALLBACK_SUCCESS, ...);
for (uint256 i = 0; i < tokens.length; i++) {
    SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i]);
    require(IERC20(tokens[i]).balanceOf(address(this)) >= balancesBefore[i], InsufficientRepayment());
}
```

This ensures the protocol's balance is fully restored regardless of token transfer mechanics.

## Proof of Concept
1. Deploy a mock ERC20 with a 1% fee on every `transfer` and `transferFrom`.
2. Create a Midnight market using this token as the loan token; supply enough to give Midnight a balance `B`.
3. Deploy `AttackerCallback` that implements `onFlashLoan` returning `CALLBACK_SUCCESS` and approves `assets` to Midnight before returning.
4. Call `midnight.flashLoan([feeToken], [B/2], attackerCallback, "")`.
5. Assert `feeToken.balanceOf(address(midnight)) < B` — balance decreased by `(B/2) * 0.01`.
6. Repeat until balance reaches zero.

Expected result: each iteration drains `fee_rate * assets` from Midnight with no revert, confirming the vulnerability. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** src/Midnight.sol (L737-752)
```text
    function flashLoan(address[] calldata tokens, uint256[] calldata assets, address callback, bytes calldata data)
        external
    {
        require(tokens.length == assets.length, InconsistentInput());
        emit EventsLib.FlashLoan(msg.sender, tokens, assets, callback);
        for (uint256 i = 0; i < tokens.length; i++) {
            SafeTransferLib.safeTransfer(tokens[i], callback, assets[i]);
        }
        require(
            IFlashLoanCallback(callback).onFlashLoan(msg.sender, tokens, assets, data) == CALLBACK_SUCCESS,
            WrongFlashLoanCallbackReturnValue()
        );
        for (uint256 i = 0; i < tokens.length; i++) {
            SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i]);
        }
    }
```

**File:** certora/specs/Solvency.spec (L31-33)
```text
    // Assume ERC20 tokens transfer correctly: no fee taking from sender or receiver, no rebasing, no blacklisting, no transfer limits.
    function _.transfer(address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, e.msg.sender, a, v) expect(bool);
    function _.transferFrom(address src, address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, src, a, v) expect(bool);
```

**File:** certora/specs/Solvency.spec (L162-163)
```text
strong invariant tokenBalanceCorrect(address token)
    tokenBalances[token][currentContract] >= collateralSum(token) + withdrawableSum(token) + claimableSettlementFee(token) - flashloans[token] - pendingFeeReceipt[token]
```
