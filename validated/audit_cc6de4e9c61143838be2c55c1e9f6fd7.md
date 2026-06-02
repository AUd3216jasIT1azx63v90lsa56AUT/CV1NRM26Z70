Based on my investigation of `src/Midnight.sol`, `live_context.json`, `SECURITY.md`, and `RESEARCHER.md`, the claim is valid.

Audit Report

## Title
Fee-on-Transfer Loan Token Breaks `withdrawable` Accounting in `liquidate()` and `repay()`, Causing Market Insolvency - (File: src/Midnight.sol)

## Summary
In `liquidate()`, `marketState[id].withdrawable` is incremented by the full nominal `repaidUnits` at line 675 before `SafeTransferLib.safeTransferFrom` is called at line 717. Because `SafeTransferLib` only checks for revert/bool return without measuring actual tokens received, a fee-on-transfer loan token causes Midnight to record more withdrawable credit than it actually holds. Lenders who withdraw later will find the contract's real balance insufficient, making the market permanently insolvent.

## Finding Description
**Root cause — `liquidate()`:**

At line 675, accounting is updated with the full nominal amount:
```solidity
_marketState.withdrawable += UtilsLib.toUint128(repaidUnits);  // line 675
_position.debt -= UtilsLib.toUint128(repaidUnits);             // line 676
```
The collateral transfer and optional callback execute, and then at line 717:
```solidity
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
```
`SafeTransferLib` only verifies the call does not revert and returns `true`; it performs no before/after balance measurement. If the loan token charges a 1% fee, Midnight receives `repaidUnits * 0.99` but `withdrawable` was increased by `repaidUnits`. The same pattern exists in `repay()`.

**`touchMarket()` imposes no restriction on `loanToken`:** Any ERC20 address is accepted. The protocol explicitly lists "arbitrary loan token" as a design feature in `live_context.json`.

**Exploit flow:**
1. Attacker calls `touchMarket(market)` where `market.loanToken` is a fee-on-transfer ERC20 (e.g., 1% fee). Market creation is permissionless — attacker can deploy their own fee-on-transfer token.
2. Lenders supply credit; borrowers take debt via normal `take()` calls.
3. Borrower's position becomes liquidatable.
4. Liquidator calls `liquidate(...)` with `repaidUnits = X`.
5. Line 675: `_marketState.withdrawable += X` — accounting records full `X`.
6. Line 676: `_position.debt -= X` — borrower's debt fully reduced.
7. Line 717: `safeTransferFrom(loanToken, payer, address(this), X)` — fee-on-transfer token delivers only `X * 0.99`. Call succeeds.
8. Post-call: `withdrawable` exceeds actual `loanToken` balance by `X * 0.01`. Gap widens with every subsequent `liquidate()` or `repay()` call.

**Existing checks are insufficient:** `SECURITY.md` does not exclude fee-on-transfer tokens. `live_context.json` line 233 explicitly states "fee-on-transfer, rebasing, false-return, ERC777-like hooks, and non-standard decimals should be tested if not explicitly excluded," and line 232 lists "ERC20 transfer deltas must match accounting deltas" as a core invariant. No code-level guard prevents a fee-on-transfer token from being used as `loanToken`.

## Impact Explanation
After one or more liquidations or repayments with a fee-on-transfer loan token, `marketState[id].withdrawable` permanently exceeds the contract's actual `loanToken` balance. Lenders calling `withdraw()` trigger `safeTransfer(loanToken, receiver, units)` — early withdrawers succeed; later lenders find the real balance insufficient and their calls revert. The market is insolvent: lenders collectively cannot redeem all their credit. The shortfall equals the cumulative fee taken across all affected `liquidate()` and `repay()` calls. This matches "protocol insolvency" and "credit/debt accounting corruption" — both listed as highest-priority impact classes in `live_context.json`.

## Likelihood Explanation
No privileged action is required. Market creation is fully permissionless — any user can call `touchMarket()` with any ERC20 as `loanToken`, including a freshly deployed fee-on-transfer token. The attacker can simultaneously be the market creator, a lender, a borrower, and a liquidator. The exploit is repeatable: every `liquidate()` or `repay()` call with `repaidUnits > 0` widens the accounting gap. Real deployed fee-on-transfer tokens (STA with 1% burn, PAXG with transfer fee) exist, and the attacker need not rely on them — they can deploy their own.

## Recommendation
Measure the actual token balance delta around each `safeTransferFrom` call and use the measured delta (not the nominal amount) to update `withdrawable`. Alternatively, explicitly document and enforce that fee-on-transfer tokens are not supported as `loanToken`, and add a validation check in `touchMarket()` that reverts if the token charges a fee (e.g., by performing a test transfer and comparing before/after balances). The former approach is more robust and consistent with the protocol's "arbitrary loan token" design goal.

## Proof of Concept
**Foundry test plan:**
1. Deploy a mock ERC20 with a 1% fee-on-transfer applied in `_transfer`.
2. Call `touchMarket()` with this token as `loanToken`.
3. Have a lender supply credit and a borrower take debt.
4. Manipulate oracle price to make the borrower liquidatable.
5. Call `liquidate()` with `repaidUnits = 1000e18`.
6. Assert: `marketState[id].withdrawable == 1000e18` but `IERC20(loanToken).balanceOf(address(midnight)) == 990e18`.
7. Have the lender call `withdraw()` for the full `withdrawable` amount — assert it reverts due to insufficient balance.
8. Repeat steps 5–7 to show the gap compounds with each liquidation. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/Midnight.sol (L675-677)
```text
            _marketState.withdrawable += UtilsLib.toUint128(repaidUnits);
            _position.debt -= UtilsLib.toUint128(repaidUnits);
        }
```

**File:** src/Midnight.sol (L717-717)
```text
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
```

**File:** live_context.json (L17-17)
```json
      "arbitrary loan token",
```

**File:** live_context.json (L231-234)
```json
      "callbacks must not corrupt partial state through reentrancy",
      "ERC20 transfer deltas must match accounting deltas",
      "fee-on-transfer, rebasing, false-return, ERC777-like hooks, and non-standard decimals should be tested if not explicitly excluded",
      "multicall must not bypass per-action invariants"
```

**File:** live_context.json (L385-392)
```json
    "external_behavior": [
      "callback reverts",
      "callback reenters",
      "token returns false",
      "token charges fee",
      "token rebases",
      "token has 6/8/18/27 decimals",
      "receiver is contract",
```
