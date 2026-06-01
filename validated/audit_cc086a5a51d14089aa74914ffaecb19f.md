Audit Report

## Title
Fee-on-Transfer `loanToken` Causes `claimableSettlementFee` Overcount Leading to Insolvency - (File: src/Midnight.sol)

## Summary
In `take()`, `claimableSettlementFee[offer.market.loanToken]` is incremented by the full `buyerAssets - sellerAssets` at line 418 before the inbound transfer executes at line 455. When `loanToken` is a fee-on-transfer ERC20, the contract receives less than the recorded amount, creating a permanent shortfall. Subsequent `claimSettlementFee()` calls will either revert (freezing fee collection) or drain tokens belonging to lenders' `withdrawable` reserves, breaking the core solvency invariant.

## Finding Description

**Exact code path:**

`src/Midnight.sol` line 418 records the full settlement fee before any transfer: [1](#0-0) 

Lines 455–456 execute the actual inbound transfer after the accounting update: [2](#0-1) 

With a fee-on-transfer token charging `f%`, the contract receives `(buyerAssets - sellerAssets) * (1 - f)` but `claimableSettlementFee` is incremented by the full `buyerAssets - sellerAssets`. No balance-before/after check surrounds line 455 to detect the shortfall.

**Why existing checks fail:**

`SafeTransferLib.safeTransferFrom` validates only success/return value, not the amount actually credited to the recipient. The Certora `Solvency.spec` `tokenBalanceCorrect` strong invariant and `pendingFeeReceiptZero` weak invariant are proved under a CVL model that explicitly assumes no fee-on-transfer behavior: [3](#0-2) 

The `CVL_transferFrom` summary transfers the exact requested amount to the destination, so the formal proofs provide no coverage for fee-on-transfer tokens: [4](#0-3) 

The `pendingFeeReceipt` ghost tracks the gap between the `claimableSettlementFee` increment and the inbound transfer, but it is cleared only when the transfer delivers the exact expected amount — which never happens with a fee-on-transfer token, leaving `pendingFeeReceipt` permanently nonzero and the solvency invariant permanently violated: [5](#0-4) 

## Impact Explanation

After a single `take()` with a 1% fee-on-transfer `loanToken` and settlement fee `F = buyerAssets - sellerAssets`:

- `claimableSettlementFee[loanToken]` increases by `F`
- Contract balance increases by `F * 0.99`
- Shortfall: `F * 0.01`

The shortfall compounds linearly on every subsequent `take()`. When `claimSettlementFee()` is called, it subtracts from `claimableSettlementFee` and calls `safeTransfer` for the full recorded amount. If the shortfall is not covered by other deposits, `safeTransfer` reverts (fee claimer DoS), or it drains tokens belonging to lenders' `withdrawable` reserves, directly breaking the core solvency invariant:

`balance >= collateralSum + withdrawableSum + claimableSettlementFee` [6](#0-5) 

This constitutes direct loss of lender funds and/or permanent freeze of protocol fee collection — both in-scope critical impacts per `live_context.json`. [7](#0-6) 

## Likelihood Explanation

**Preconditions:**
1. `loanToken` is a fee-on-transfer ERC20 — such tokens exist in production (USDT with fee enabled, STA, PAXG). The protocol explicitly supports "arbitrary loan token" as a design feature.
2. `feeSetter` has set a nonzero settlement fee for that token — a routine protocol operation.
3. Any taker calls `take()` with nonzero `units` at a tick where `buyerAssets > sellerAssets`.

The protocol's own `live_context.json` lists "fee-on-transfer" under `external_calls` core invariants as something that "should be tested if not explicitly excluded," and lists "Can non-standard token behavior break accounting assumptions?" as a highest-priority audit question. SECURITY.md contains no explicit exclusion of fee-on-transfer tokens. [8](#0-7) [9](#0-8) 

No privileged access is required after the `feeSetter` performs the routine fee configuration. The exploit is repeatable on every `take()` call.

## Recommendation

Wrap the inbound settlement fee transfer with a balance-before/after check and use the actual received amount to update `claimableSettlementFee`:

```solidity
uint256 balanceBefore = IERC20(offer.market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
uint256 received = IERC20(offer.market.loanToken).balanceOf(address(this)) - balanceBefore;
claimableSettlementFee[offer.market.loanToken] += received;
```

Alternatively, explicitly document and enforce that fee-on-transfer tokens are not supported as `loanToken` (e.g., via a validation check in `touchMarket` or a documented exclusion in SECURITY.md), and add a corresponding Certora rule that rejects such tokens.

## Proof of Concept

**Foundry test outline:**

1. Deploy a mock ERC20 with a 1% fee-on-transfer on all `transferFrom` calls.
2. Set it as `loanToken` in a market via `touchMarket`.
3. Call `setDefaultSettlementFee` to set a nonzero fee for the token (as `feeSetter`).
4. Call `take()` with `units` such that `buyerAssets - sellerAssets = 1000e18`.
5. Assert: `claimableSettlementFee[loanToken] == 1000e18` (overcounted).
6. Assert: `IERC20(loanToken).balanceOf(address(midnight)) == 990e18` (actual received).
7. Call `claimSettlementFee()` for the full `1000e18`.
8. Assert: the call reverts if no other balance covers the shortfall, OR that lender `withdrawable` is reduced by `10e18` if it does — demonstrating insolvency. [10](#0-9) [2](#0-1)

### Citations

**File:** src/Midnight.sol (L416-418)
```text
        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** certora/specs/Solvency.spec (L31-33)
```text
    // Assume ERC20 tokens transfer correctly: no fee taking from sender or receiver, no rebasing, no blacklisting, no transfer limits.
    function _.transfer(address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, e.msg.sender, a, v) expect(bool);
    function _.transferFrom(address src, address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, src, a, v) expect(bool);
```

**File:** certora/specs/Solvency.spec (L43-59)
```text
function CVL_transferFrom(env e, address token, address src, address dest, uint256 value) returns bool {
    if (tokenBalances[token][src] < value || tokenBalances[token][dest] + value >= 2 ^ 256) {
        revert();
    }

    // Non-deterministically set success, which allows to simulate permissions.
    bool success;
    if (success) {
        tokenBalances[token][src] = assert_uint256(tokenBalances[token][src] - value);
        tokenBalances[token][dest] = assert_uint256(tokenBalances[token][dest] + value);
    
        // Settle pending settlement fee receipts only on the exact fee transfer expected by take().
        if (dest == currentContract && pendingFeeReceipt[token] == to_mathint(value)) {
            pendingFeeReceipt[token] = 0;
        }
    }
    return success;
```

**File:** certora/specs/Solvency.spec (L140-151)
```text
// Settlement fee receipts pending settlement: claimableSettlementFee is incremented in take before
// the inbound fee transfer happens, so we track the gap and clear it in CVL_transferFrom.
persistent ghost mapping(address => mathint) pendingFeeReceipt {
    init_state axiom (forall address token. pendingFeeReceipt[token] == 0);
}

hook Sstore claimableSettlementFee[KEY address token] uint256 newVal (uint256 oldVal) {
    // Except for claimSettlementFee, the claimableSettlementFee is non-decreasing, see WithdrawableMonotonicity.spec.
    if (newVal > oldVal) {
        pendingFeeReceipt[token] = pendingFeeReceipt[token] + newVal - oldVal;
    }
}
```

**File:** certora/specs/Solvency.spec (L162-163)
```text
strong invariant tokenBalanceCorrect(address token)
    tokenBalances[token][currentContract] >= collateralSum(token) + withdrawableSum(token) + claimableSettlementFee(token) - flashloans[token] - pendingFeeReceipt[token]
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
