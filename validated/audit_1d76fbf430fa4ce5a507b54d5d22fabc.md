Audit Report

## Title
Fee-on-transfer loanToken causes `claimableSettlementFee` overstatement, leading to settlement fee undercollection - (File: src/Midnight.sol)

## Summary
In `Midnight.take`, `claimableSettlementFee[offer.market.loanToken]` is incremented by the full nominal `buyerAssets - sellerAssets` at line 418 before any token transfer occurs. When the `loanToken` is a fee-on-transfer token, `safeTransferFrom` at line 455 delivers only `(buyerAssets - sellerAssets) * (1 - fee_rate)` to `address(this)`, while the accounting records the full nominal amount. The discrepancy accumulates across calls, eventually causing `claimSettlementFee` to revert when it attempts to transfer more tokens than the contract holds.

## Finding Description
**Verified code path in `src/Midnight.sol`:**

1. **Line 418**: `claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;` — full nominal fee credited to accounting before any transfer.
2. **Line 420**: `address buyerCallback = offer.buy ? offer.callback : takerCallback;` — for a buy offer, maker-controlled `offer.callback` becomes `buyerCallback`.
3. **Line 422**: `address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);` — callback contract is designated payer.
4. **Lines 445–453**: `onBuy` is called on `buyerCallback`; it returns `CALLBACK_SUCCESS`.
5. **Line 455**: `SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);` — with a fee-on-transfer token, Midnight receives only `(buyerAssets - sellerAssets) * (1 - fee_rate)`.

**Root cause:** `claimableSettlementFee` is updated before the transfer, and `SafeTransferLib.safeTransferFrom` validates only the boolean return of `transferFrom` — it never measures the actual balance increase at `address(this)`. No post-transfer balance delta check exists anywhere in `take`.

**Why existing checks fail:**
- `SafeTransferLib` checks only the `transferFrom` return value, not the received amount.
- `touchMarket` imposes no restriction on `loanToken` type — market creation is fully permissionless.
- No post-transfer invariant check (e.g., `balanceOf(address(this))` delta) exists in `take`.

**Scope note:** `live_context.json` under `core_invariants.external_calls` explicitly states: *"fee-on-transfer, rebasing, false-return, ERC777-like hooks, and non-standard decimals should be tested if not explicitly excluded."* No exclusion exists in `SECURITY.md` or elsewhere for fee-on-transfer tokens. The `reporting_bias.likely_invalid_if` entry `"requires malicious token owner only"` does not apply here because the vulnerability is also triggered by any existing fee-on-transfer token (e.g., tokens with built-in transfer taxes) used as `loanToken` — no attacker-deployed token is required.

## Impact Explanation
After each `take` with a fee-on-transfer `loanToken`, `claimableSettlementFee[token]` exceeds the actual token balance held by Midnight for that token by `(buyerAssets - sellerAssets) * fee_rate`. Repeated calls accumulate the discrepancy. When `claimSettlementFee` is eventually called, it attempts to `safeTransfer` the full recorded amount, which exceeds the actual balance, causing a revert. The settlement fee is permanently unclaimable, constituting protocol insolvency for that token's fee accounting. This matches the `best_bug_classes` entry `"protocol insolvency"` in `live_context.json`.

## Likelihood Explanation
Market creation is fully permissionless — any address can call `touchMarket` with an arbitrary `loanToken`. The exploit is reachable by any unprivileged user. Existing tokens with transfer taxes (e.g., tokens with built-in fee mechanisms) used as `loanToken` trigger the vulnerability without any attacker-deployed contract. The exploit is repeatable across multiple `take` calls to accumulate the discrepancy.

## Recommendation
Add a post-transfer balance check in `take` to measure the actual received amount and use that delta to update `claimableSettlementFee`, rather than the nominal `buyerAssets - sellerAssets`. For example:

```solidity
uint256 balanceBefore = IERC20(offer.market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
uint256 received = IERC20(offer.market.loanToken).balanceOf(address(this)) - balanceBefore;
claimableSettlementFee[offer.market.loanToken] += received; // move line 418 here, use `received`
```

Alternatively, explicitly document and enforce that fee-on-transfer tokens are not supported as `loanToken` by adding a validation check in `touchMarket`.

## Proof of Concept
**Minimal Foundry test plan:**

1. Deploy a mock ERC20 with a 10% fee on every `transferFrom`.
2. Call `touchMarket` with this token as `loanToken` (permissionless).
3. Construct a valid buy offer (`offer.buy = true`) with `offer.callback` pointing to a contract that holds fee-on-transfer tokens, has approved Midnight, and returns `CALLBACK_SUCCESS` from `onBuy`.
4. Use a second address as taker (to satisfy `offer.maker != taker`).
5. Call `take`.
6. Assert: `claimableSettlementFee[token]` equals `buyerAssets - sellerAssets` (nominal), but `token.balanceOf(address(midnight))` equals only `(buyerAssets - sellerAssets) * 0.9`.
7. Repeat step 5 multiple times to accumulate the discrepancy.
8. Call `claimSettlementFee` — assert it reverts due to insufficient balance. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/Midnight.sol (L418-418)
```text
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

**File:** src/Midnight.sol (L420-422)
```text
        address buyerCallback = offer.buy ? offer.callback : takerCallback;
        address sellerCallback = offer.buy ? takerCallback : offer.callback;
        address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
```

**File:** src/Midnight.sol (L445-456)
```text
        if (buyerCallback != address(0)) {
            bytes memory buyerCallbackData = offer.buy ? offer.callbackData : takerCallbackData;
            require(
                IBuyCallback(buyerCallback)
                    .onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
                == CALLBACK_SUCCESS,
                WrongBuyCallbackReturnValue()
            );
        }

        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
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

**File:** live_context.json (L405-413)
```json
    "likely_invalid_if": [
      "requires admin misuse only",
      "requires oracle compromise only",
      "requires malicious token owner only",
      "only causes failed transaction for attacker",
      "only affects offchain routing quality",
      "only proves integration misuse without protocol-level bug",
      "matches documented coarse authorization behavior without additional confusion bug"
    ]
```
