Audit Report

## Title
Flash loan repayment with fee-on-transfer token silently under-repays, draining Midnight's reserves — (File: `src/Midnight.sol`)

## Summary
The `flashLoan` function transfers `assets[i]` out and pulls `assets[i]` back via `safeTransferFrom` without snapshotting or asserting the contract balance before and after. When the loan token charges a transfer fee, the repayment leg delivers `assets[i] − fee` to Midnight instead of `assets[i]`, causing a net loss of `fee` tokens per call. Repeated calls drain Midnight's token reserves until lender withdrawals revert.

## Finding Description
**Exact code path:** `src/Midnight.sol` lines 737–752. [1](#0-0) 

```solidity
for (uint256 i = 0; i < tokens.length; i++) {
    SafeTransferLib.safeTransfer(tokens[i], callback, assets[i]);          // out
}
require(IFlashLoanCallback(callback).onFlashLoan(...) == CALLBACK_SUCCESS, ...);
for (uint256 i = 0; i < tokens.length; i++) {
    SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i]); // in
}
```

**Root cause:** No pre/post balance invariant is enforced. The function only requires that `safeTransferFrom` does not revert; it never asserts `balanceAfter >= balanceBefore`.

**Exploit flow** (fee rate `r`, loan amount `A`, Midnight balance `B`):

| Step | Action | Midnight balance | Callback balance |
|------|--------|-----------------|-----------------|
| Pre  | Attacker pre-funds callback with `A·r` | `B` | `A·r` |
| 1    | `safeTransfer(token, callback, A)` — callback receives `A·(1−r)` | `B − A` | `A` |
| 2    | `onFlashLoan` returns `CALLBACK_SUCCESS` | `B − A` | `A` |
| 3    | `safeTransferFrom(token, callback, midnight, A)` — Midnight receives `A·(1−r)` | `B − A·r` | `0` |

Midnight ends with `B − A·r`. Repeating `N` times drains `N·A·r`.

**Why existing checks fail:** `safeTransferFrom` succeeds because the callback holds exactly `A` tokens (pre-funded `A·r` + received `A·(1−r)`), so no revert occurs. The Certora `Solvency.spec` formal verification explicitly assumes standard (non-fee) ERC20 tokens and provides no protection here — the spec comment at line 31 reads: *"Assume ERC20 tokens transfer correctly: no fee taking from sender or receiver, no rebasing, no blacklisting, no transfer limits."* [2](#0-1) 

The `live_context.json` core invariants explicitly flag this gap: *"fee-on-transfer, rebasing, false-return, ERC777-like hooks, and non-standard decimals should be tested if not explicitly excluded"* — and no explicit exclusion exists anywhere in the protocol documentation. [3](#0-2) 

## Impact Explanation
Midnight's actual token balance decreases by `A·r` per flash loan call while `withdrawable`, `collateralSum`, and `claimableSettlementFee` accounting remains unchanged. The actual balance falls below the sum of obligations tracked by the solvency invariant. Lenders attempting to `withdraw` will receive reverts once the balance is exhausted. This maps directly to the in-scope impact class of protocol insolvency and permanent fund freeze for lenders.

## Likelihood Explanation
The protocol explicitly supports `"arbitrary loan token"` and `"permissionless market creation"`. [4](#0-3) 

Fee-on-transfer tokens exist in the wild (e.g., tokens with built-in protocol fees). An attacker does not need to be the token owner — they only need a market where a fee-on-transfer token is the loan token and lenders have deposited. The attack is permissionless, requires no special role, and is repeatable indefinitely. The attacker's cost equals `A·r` per iteration (pre-funding the callback), making it a 1:1 griefing ratio that is economically viable for a motivated adversary.

## Recommendation
Replace the nominal-amount repayment check with a balance-delta assertion:

```solidity
for (uint256 i = 0; i < tokens.length; i++) {
    uint256 balanceBefore = IERC20(tokens[i]).balanceOf(address(this));
    SafeTransferLib.safeTransfer(tokens[i], callback, assets[i]);
    // ... callback ...
    SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i]);
    require(
        IERC20(tokens[i]).balanceOf(address(this)) >= balanceBefore,
        InsufficientRepayment()
    );
}
```

Alternatively, explicitly document and enforce that fee-on-transfer tokens are unsupported loan tokens (e.g., via a token allowlist or a market-creation check), and add a corresponding invariant to the Certora spec.

## Proof of Concept
**Foundry test plan:**

1. Deploy a mock ERC20 with a 1% transfer fee (deducted from the recipient on every transfer).
2. Deploy Midnight and create a market with this token as the loan token.
3. Lender deposits `1000e18` tokens into Midnight.
4. Attacker deploys a callback contract that pre-funds itself with `10e18` (= `1000e18 · 0.01`) and returns `CALLBACK_SUCCESS`.
5. Attacker calls `flashLoan([token], [1000e18], callback, "")`.
6. Assert `token.balanceOf(address(midnight)) == 990e18` (drained by `10e18`).
7. Repeat 99 more times; assert `token.balanceOf(address(midnight)) == 0` while lender's recorded credit remains `1000e18`.
8. Lender calls `withdraw`; assert it reverts due to insufficient balance.

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

**File:** live_context.json (L13-27)
```json
    "main_design": [
      "isolated immutable markets",
      "permissionless market creation",
      "fixed maturity per market",
      "arbitrary loan token",
      "single or multi-collateral markets",
      "credit units for lenders",
      "debt units for borrowers",
      "offer-based trading instead of pooled liquidity",
      "makers do not lock capital when publishing offers",
      "takers execute offers onchain",
      "ratifier validates offer authority",
      "optional maker callback can source funds during take",
      "optional market gates restrict entry or liquidation"
    ]
```

**File:** live_context.json (L231-234)
```json
      "callbacks must not corrupt partial state through reentrancy",
      "ERC20 transfer deltas must match accounting deltas",
      "fee-on-transfer, rebasing, false-return, ERC777-like hooks, and non-standard decimals should be tested if not explicitly excluded",
      "multicall must not bypass per-action invariants"
```
