Audit Report

## Title
Fee-on-Transfer `loanToken` in `repay` Inflates `withdrawable` Beyond Actual Balance - (File: src/Midnight.sol)

## Summary
The `repay` function credits the full `units` to `marketState[id].withdrawable` at line 509 before executing the token transfer at line 520. When a fee-on-transfer token is used as `loanToken`, only `units * (1 - fee_rate)` tokens are actually received, creating a permanent gap between the recorded `withdrawable` and the real token balance. This gap grows monotonically with every `repay` call, eventually causing later lenders' `withdraw` calls to revert due to insufficient contract balance.

## Finding Description
**Root cause — `repay` (lines 508–520):**

`marketState[id].withdrawable += UtilsLib.toUint128(units)` executes at line 509, crediting the full `units` to withdrawable accounting before any token transfer occurs. The actual receipt via `SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units)` at line 520 is the last operation. For a fee-on-transfer token, only `units * (1 - fee_rate)` tokens arrive, but the full `units` is already permanently recorded.

**`SafeTransferLib.safeTransferFrom` (lines 24–34):**

The library calls `transferFrom` and checks only that the call succeeds and that the return value (if any) decodes to `true`. There is no pre/post balance snapshot. A fee-on-transfer token satisfies this check while delivering fewer tokens than requested.

**`touchMarket` (lines 755–791):**

Market creation validates maturity, collateral sorting, LLTV tiers, and `maxLif`. There is no restriction on `loanToken`; any ERC20-compatible address, including a fee-on-transfer token, is accepted. Market creation is permissionless.

**`withdraw` (lines 481–500):**

`withdraw` decrements `_marketState.withdrawable` by `units` at line 494 and calls `SafeTransferLib.safeTransfer(market.loanToken, receiver, units)` at line 499. Once the real token balance falls below `withdrawable` due to accumulated fee gaps, the `safeTransfer` call reverts for later lenders, permanently freezing their credited funds.

**Exploit flow:**
1. Any unprivileged actor calls `touchMarket` with a fee-on-transfer ERC20 as `loanToken`.
2. Lenders supply credit; borrowers accumulate debt via normal `take` interactions.
3. Borrower calls `repay(market, N, onBehalf, address(0), '')`.
4. Line 509: `withdrawable += N` — full `N` recorded.
5. Line 520: contract receives only `N * (1 - f)` tokens.
6. Gap = `N * f` tokens per repayment; cumulative and monotonically growing.
7. Early lenders calling `withdraw` drain the real balance; later lenders' `withdraw` calls revert.

**Existing checks and why they fail:**

`SafeTransferLib` only validates the boolean return of `transferFrom`, not the actual token delta. `touchMarket` imposes no token-type restriction. There is no post-transfer balance check anywhere in `repay`.

## Impact Explanation
The accounting invariant `token_balance(address(this)) >= withdrawable` is permanently violated. Lenders who withdraw first are made whole; subsequent lenders find the contract insolvent and their funds are irreversibly frozen. The shortfall equals the cumulative fee deducted across all repayments and grows with every `repay` call. This constitutes a permanent, partial freeze of user funds — matching the "Permanent lock, freeze, or unrecoverable corruption of user/project state" impact category in RESEARCHER.md.

## Likelihood Explanation
Market creation is permissionless via `touchMarket`; no privileged key is required. Fee-on-transfer tokens are a well-known, deployed ERC20 variant. The bug is triggered on every `repay` call in such a market, making it repeatable and cumulative. No victim mistake is required — lenders interact with a market that appears legitimate. The only precondition is the existence of a market with a fee-on-transfer `loanToken`, which any external actor can create.

## Recommendation
Add a post-transfer balance check in `repay` to measure the actual tokens received and credit only that amount to `withdrawable`:

```solidity
uint256 balanceBefore = IERC20(market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
uint256 received = IERC20(market.loanToken).balanceOf(address(this)) - balanceBefore;
marketState[id].withdrawable += UtilsLib.toUint128(received);
```

Alternatively, restrict `touchMarket` to a whitelist of approved `loanToken` addresses that are known not to have transfer fees, or add an explicit check that `received == units` and revert otherwise to prevent fee-on-transfer tokens from being used at all.

## Proof of Concept
1. Deploy a mock ERC20 with a 1% transfer fee (deducts 1% from recipient on every `transferFrom`).
2. Call `touchMarket` with this token as `loanToken` — succeeds with no restriction.
3. Lender calls `supply` with 1000 tokens; `withdrawable = 1000`, contract balance = 990 (fee on supply too, but focus on repay).
4. Borrower calls `take` to borrow; then calls `repay(market, 1000, ...)`.
5. After `repay`: `withdrawable += 1000` (line 509), but contract receives only 990 tokens (line 520).
6. Repeat repay calls; gap grows by 10 tokens per 1000-unit repayment.
7. First lender withdraws 990 tokens successfully; second lender's `withdraw` for any amount causes `safeTransfer` to revert — funds frozen.

A Foundry fuzz test asserting `IERC20(loanToken).balanceOf(address(midnight)) >= marketState[id].withdrawable` after any sequence of `repay` calls will fail immediately with a fee-on-transfer token.