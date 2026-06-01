Audit Report

## Title
Missing zero-value guard on refund `safeTransfer` causes revert for tokens that reject zero-value transfers - (File: `src/periphery/MidnightBundles.sol`)

## Summary
In `buyWithUnitsTargetAndWithdrawCollateral`, line 104 unconditionally calls `SafeTransferLib.safeTransfer(loanToken, msg.sender, maxBuyerAssets - filledBuyerAssets - referralFeeAssets)` with no zero-value guard. When `maxBuyerAssets` equals the exact total cost (`filledBuyerAssets + referralFeeAssets`), the refund amount is zero. For any loan token that reverts on zero-value ERC-20 transfers, `SafeTransferLib.safeTransfer` propagates the revert, undoing all successful takes in the bundle. The immediately preceding referral fee transfer on line 103 is correctly guarded with `if (referralFeeAssets > 0)`, making the omission on line 104 a clear inconsistency and a defect.

## Finding Description

**Root cause:** `SafeTransferLib.safeTransfer` (lines 12–22 of `src/libraries/SafeTransferLib.sol`) unconditionally issues `token.call(abi.encodeCall(IERC20.transfer, (to, value)))` regardless of whether `value` is zero. If the token's `transfer` reverts (e.g., due to an internal `require(amount > 0)` guard), `success` is `false` and the assembly block re-reverts with the token's revert data.

**Affected code path:**
```solidity
// MidnightBundles.sol lines 102–104
uint256 referralFeeAssets = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets); // guarded
SafeTransferLib.safeTransfer(loanToken, msg.sender, maxBuyerAssets - filledBuyerAssets - referralFeeAssets); // NOT guarded
```

**Exploit flow:**
1. Caller invokes `buyWithUnitsTargetAndWithdrawCollateral` with a loan token that reverts on zero-value transfers.
2. Caller sets `maxBuyerAssets` to the exact expected cost (e.g., `filledBuyerAssets` when `referralFeePct == 0`, or `filledBuyerAssets + referralFeeAssets` otherwise). This is the natural behavior of any frontend or contract computing the exact cost.
3. All takes succeed; `filledBuyerAssets` accumulates correctly; collateral withdrawals execute.
4. Line 102 computes `referralFeeAssets`; line 103's guarded transfer executes or is skipped.
5. Line 104 computes `maxBuyerAssets - filledBuyerAssets - referralFeeAssets = 0` and calls `safeTransfer(..., 0)`.
6. The token reverts; the entire transaction reverts, undoing all takes and collateral withdrawals.

**Why existing checks fail:** The `if (referralFeeAssets > 0)` guard on line 103 demonstrates the developer was aware of zero-value transfer risks, making the absence of an equivalent guard on line 104 an oversight rather than a design choice. The NatSpec comment on line 26 ("Zero checks are not systematically performed") does not override the inconsistency introduced by line 103's guard.

## Impact Explanation
The entire bundle transaction reverts despite all takes having succeeded, meaning the taker cannot complete the intended buy-and-withdraw operation. For time-sensitive operations (filling before a deadline, capturing a specific price), the revert carries concrete economic cost beyond gas. The bug is triggered by normal, intended usage — setting `maxBuyerAssets` to the exact expected cost — and is repeatable on every call with a matching token and exact `maxBuyerAssets`.

## Likelihood Explanation
Two independent preconditions are required:
1. The loan token reverts on zero-value transfers. This is a known behavior in several deployed ERC-20 tokens (e.g., tokens with explicit `require(amount > 0)` guards).
2. The caller sets `maxBuyerAssets` to the exact total cost. This is the natural behavior of any frontend or smart contract integration that pre-computes the expected spend.

Both preconditions are reachable by any unprivileged user without oracle manipulation, privileged access, or user error. The scenario is deterministic and repeatable.

## Recommendation
Add a zero-value guard on line 104, consistent with the guard already present on line 103:

```solidity
uint256 refundAssets = maxBuyerAssets - filledBuyerAssets - referralFeeAssets;
if (refundAssets > 0) SafeTransferLib.safeTransfer(loanToken, msg.sender, refundAssets);
```

This mirrors the pattern already used for the referral fee transfer and eliminates the inconsistency.

## Proof of Concept
1. Deploy a mock ERC-20 token with `require(amount > 0, "zero transfer")` in its `transfer` function.
2. Deploy `MidnightBundles` with a `Midnight` instance using this token as the loan token.
3. Call `buyWithUnitsTargetAndWithdrawCollateral` with `referralFeePct = 0` and `maxBuyerAssets` set to the exact cost of the takes (i.e., equal to the sum of `resBuyerAssets` returned by each take).
4. Observe: all takes succeed internally, but the transaction reverts at line 104 when `safeTransfer` is called with `value = 0`.
5. Confirm: adding `if (maxBuyerAssets - filledBuyerAssets - referralFeeAssets > 0)` guard before line 104 resolves the revert. [1](#0-0) [2](#0-1)

### Citations

**File:** src/periphery/MidnightBundles.sol (L102-104)
```text
        uint256 referralFeeAssets = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
        if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
        SafeTransferLib.safeTransfer(loanToken, msg.sender, maxBuyerAssets - filledBuyerAssets - referralFeeAssets);
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
