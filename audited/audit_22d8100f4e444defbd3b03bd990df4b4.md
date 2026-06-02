The code at lines 737–752 of `src/Midnight.sol` confirms the exact behavior described. The Certora `Solvency.spec` line 31 explicitly excludes fee-on-transfer tokens from its assumptions. SECURITY.md contains no exclusion for this class of issue. The attack is triggerable by an unprivileged caller with no special role.

---

Audit Report

## Title
`flashLoan` is not balance-neutral for fee-on-transfer tokens, causing direct loss of protocol funds - (File: src/Midnight.sol)

## Summary
`Midnight.flashLoan` transfers out `assets[i]` to the callback and then pulls back exactly `assets[i]` via `safeTransferFrom` with no pre/post balance snapshot. For fee-on-transfer tokens, the inbound `safeTransferFrom` at line 750 delivers only `assets[i] * (1 - fee)` to Midnight, producing a net balance decrease of `assets[i] * fee` per call. Any unprivileged caller can repeat this until Midnight's entire FOT-token reserve is drained.

## Finding Description
Full execution path in `flashLoan` (lines 737–752, `src/Midnight.sol`):

1. **Line 743** – `SafeTransferLib.safeTransfer(tokens[i], callback, assets[i])`: Midnight's balance decreases by exactly `assets[i]`; the callback receives `assets[i] * (1 - fee)` due to the token's transfer fee.
2. **Lines 745–748** – callback must return `CALLBACK_SUCCESS`.
3. **Line 750** – `SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i])`: the token deducts its fee from the transferred amount, so Midnight receives only `assets[i] * (1 - fee)` despite requesting `assets[i]`.

Net balance change for Midnight: `-assets[i] + assets[i]*(1-fee) = -assets[i]*fee`.

There is no pre/post balance snapshot, no minimum-received check, and no token whitelist. The Certora `Solvency.spec` formal model explicitly excludes this case at line 31 with the assumption *"no fee taking from sender or receiver"*, so the formal proofs provide no coverage here.

For the `safeTransferFrom` to succeed, the callback must hold at least `assets[i]` tokens. Since it only received `assets[i]*(1-fee)` on the outbound leg, the attacker must pre-fund `assets[i]*fee` extra. The attacker's cost per iteration equals Midnight's loss per iteration. A griefing attacker can drain Midnight's entire FOT-token reserve by looping `flashLoan` calls with the maximum available balance.

## Impact Explanation
Every `flashLoan` call with a fee-on-transfer token permanently removes `assets[i] * fee` tokens from Midnight's balance. These tokens are not accounted for in any market state, making the loss unrecoverable. Repeated calls drain the full FOT-token reserve held by the protocol, directly violating the invariant that contract balances cover collateral, credit redemption, fees, and withdrawable assets. This constitutes direct theft/loss of protocol-held assets.

## Likelihood Explanation
- **Preconditions**: Midnight must hold a non-zero balance of a fee-on-transfer token (e.g., deposited as collateral or loan token in a market). The attacker needs no special role.
- **Feasibility**: Any externally-owned account can call `flashLoan` with no prerequisites beyond holding enough of the FOT token to cover the fee on the repayment leg.
- **Repeatability**: The attack is fully repeatable in a loop until Midnight's balance of the token is exhausted.

## Recommendation
Add a pre/post balance check around the repayment leg to enforce that Midnight's balance is restored to at least its pre-loan level:

```solidity
// Before the outbound loop, snapshot balances:
uint256[] memory balancesBefore = new uint256[](tokens.length);
for (uint256 i = 0; i < tokens.length; i++) {
    balancesBefore[i] = IERC20(tokens[i]).balanceOf(address(this));
}
// ... existing transfer-out and callback ...
// After the repayment loop, verify balances:
for (uint256 i = 0; i < tokens.length; i++) {
    require(
        IERC20(tokens[i]).balanceOf(address(this)) >= balancesBefore[i],
        InsufficientRepayment()
    );
}
```

Alternatively, maintain an explicit token whitelist that excludes fee-on-transfer tokens from `flashLoan`.

## Proof of Concept
1. Deploy a standard fee-on-transfer ERC20 token with a 1% transfer fee.
2. Seed a Midnight market using this token as the loan token so Midnight holds a balance (e.g., 1000 tokens).
3. Deploy a malicious callback contract that pre-funds `assets * fee` (10 tokens) and returns `CALLBACK_SUCCESS`.
4. Call `flashLoan([token], [1000], maliciousCallback, "")`.
5. Observe: Midnight sent 1000, received 990 (1% fee deducted on inbound), net loss = 10 tokens.
6. Repeat until Midnight's balance is zero.

Expected result: Midnight's token balance decreases by `assets * fee` per iteration; the attacker's cost equals the protocol's loss, confirming a griefing drain path.