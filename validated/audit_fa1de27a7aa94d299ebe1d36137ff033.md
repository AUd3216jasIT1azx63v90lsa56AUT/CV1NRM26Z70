### Title
Referral Fee Burned When `referralFeeRecipient` Is `address(0)` — (File: src/periphery/MidnightBundles.sol)

---

### Summary

All five bundle functions in `MidnightBundles` compute a `referralFeeAssets` amount and transfer it to a caller-supplied `referralFeeRecipient` without validating that the recipient is not `address(0)`. If `referralFeePct > 0` but `referralFeeRecipient == address(0)` — a realistic integration/frontend mistake — the referral fee is either permanently burned (for tokens permitting zero-address transfers) or the entire transaction reverts (for standard ERC-20 tokens), causing a DoS on the bundle.

---

### Finding Description

In `MidnightBundles.sol`, every bundle entry-point follows the same pattern:

```solidity
if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
```

The five affected functions are:
- `buyWithUnitsTargetAndWithdrawCollateral` — [1](#0-0) 
- `supplyCollateralAndSellWithUnitsTarget` — [2](#0-1) 
- `buyWithAssetsTargetAndWithdrawCollateral` — [3](#0-2) 
- `supplyCollateralAndSellWithAssetsTarget` — [4](#0-3) 
- `repayAndWithdrawCollateral` — [5](#0-4) 

The only guard present is `require(referralFeePct < WAD, PctExceeded())` [6](#0-5)  — there is no `require(referralFeeRecipient != address(0) || referralFeePct == 0)` guard anywhere.

`SafeTransferLib.safeTransfer` delegates directly to the token's `transfer(to, value)` call without any zero-address pre-check: [7](#0-6) 

The root cause is structurally identical to the external report: a fee recipient address is used in a transfer without a zero-address guard. In the external report the recipient came from an unset mapping; here it comes from an unvalidated function parameter.

---

### Impact Explanation

Two concrete outcomes depending on the loan token:

1. **Token allows transfer to `address(0)`** (non-standard but in-scope per Midnight's token safety requirements, which only require no-revert on no-op, not on zero-address transfers): `referralFeeAssets` are permanently burned. The caller (`msg.sender`) loses those funds — they paid `maxBuyerAssets` but receive back `maxBuyerAssets - filledBuyerAssets - referralFeeAssets` instead of `maxBuyerAssets - filledBuyerAssets`. [8](#0-7) 

2. **Standard ERC-20 token (reverts on zero-address transfer)**: The entire bundle transaction reverts, causing a complete DoS on the bundle operation. The taker's collateral supplies and order fills are rolled back.

---

### Likelihood Explanation

**Low.** Requires `referralFeePct > 0` to be set alongside `referralFeeRecipient == address(0)`. This is a realistic frontend/integration mistake: a developer enables referral fees but forgets to populate the recipient address (e.g., leaves it as the zero-value default in a struct or omits it from a config). No privileged access is required — any caller of the bundle functions can trigger this.

---

### Recommendation

Add a zero-address guard at the top of each bundle function (or in a shared internal helper):

```solidity
require(referralFeePct == 0 || referralFeeRecipient != address(0), InvalidReferralFeeRecipient());
```

This mirrors the fix recommended in the external report: validate the recipient before any fee transfer is attempted. [9](#0-8) 

---

### Proof of Concept

1. Deploy `MidnightBundles` pointing at a live `Midnight` instance.
2. Create a valid market and a set of sell offers.
3. Call `buyWithUnitsTargetAndWithdrawCollateral` with:
   - `referralFeePct = 1e16` (1%)
   - `referralFeeRecipient = address(0)`
   - valid `targetUnits`, `maxBuyerAssets`, `taker`, `takes[]`
4. **For a burn-permitting token**: transaction succeeds; `referralFeeAssets = filledBuyerAssets * 1e16 / (WAD - 1e16)` is sent to `address(0)` and permanently lost. `msg.sender` receives `maxBuyerAssets - filledBuyerAssets - referralFeeAssets` instead of `maxBuyerAssets - filledBuyerAssets`. [8](#0-7) 
5. **For a standard ERC-20**: `SafeTransferLib.safeTransfer(loanToken, address(0), referralFeeAssets)` reverts inside the token, reverting the entire bundle. [10](#0-9)

### Citations

**File:** src/periphery/MidnightBundles.sol (L59-62)
```text
    ) external {
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
        require(referralFeePct < WAD, PctExceeded());
        address loanToken = takes[0].offer.market.loanToken;
```

**File:** src/periphery/MidnightBundles.sol (L102-104)
```text
        uint256 referralFeeAssets = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
        if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
        SafeTransferLib.safeTransfer(loanToken, msg.sender, maxBuyerAssets - filledBuyerAssets - referralFeeAssets);
```

**File:** src/periphery/MidnightBundles.sol (L167-167)
```text
        if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
```

**File:** src/periphery/MidnightBundles.sol (L239-239)
```text
        if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
```

**File:** src/periphery/MidnightBundles.sol (L306-306)
```text
        if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
```

**File:** src/periphery/MidnightBundles.sol (L347-347)
```text
        if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
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
