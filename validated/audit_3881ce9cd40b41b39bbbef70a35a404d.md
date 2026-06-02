Audit Report

## Title
Referral fee not budgeted within `maxBuyerAssets`, causing revert that blocks a valid fill - (File: src/periphery/MidnightBundles.sol)

## Summary
In `buyWithUnitsTargetAndWithdrawCollateral`, the bundler pulls exactly `maxBuyerAssets` from `msg.sender` and the take loop drains `filledBuyerAssets` of that balance. The referral fee (`referralFeeAssets = filledBuyerAssets * referralFeePct / (WAD - referralFeePct)`) is then computed on top and transferred from the bundler's remaining balance. When `maxBuyerAssets == filledBuyerAssets` and `referralFeePct > 0`, the bundler holds zero tokens, causing the `safeTransfer` at line 103 to revert with an ERC20 insufficient-balance error and rolling back the entire transaction.

## Finding Description
**Code path in `src/periphery/MidnightBundles.sol`, function `buyWithUnitsTargetAndWithdrawCollateral`:**

1. **Line 66** pulls exactly `maxBuyerAssets` from `msg.sender` into the bundler. **Line 67** grants `type(uint256).max` approval to MIDNIGHT. [1](#0-0) 

2. **Lines 79–85**: The take loop calls `MIDNIGHT.take(...)`, which pulls `resBuyerAssets` from the bundler per iteration, accumulating into `filledBuyerAssets`. After the loop, the bundler holds `maxBuyerAssets - filledBuyerAssets` tokens. [2](#0-1) 

3. **Line 102**: `referralFeeAssets = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct)`. When `referralFeePct > 0` and `filledBuyerAssets > 0`, this is strictly positive. [3](#0-2) 

4. **Line 103**: `if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets)`. If `maxBuyerAssets == filledBuyerAssets`, the bundler's balance is zero; the ERC20 transfer fails and the revert propagates. [4](#0-3) 

5. **Line 104**: `SafeTransferLib.safeTransfer(loanToken, msg.sender, maxBuyerAssets - filledBuyerAssets - referralFeeAssets)` would also underflow under Solidity 0.8.34 checked arithmetic, but is never reached because line 103 reverts first. [5](#0-4) 

**Root cause:** No check enforces `maxBuyerAssets >= filledBuyerAssets + referralFeeAssets`. The NatSpec at lines 47–48 correctly documents the total cost formula (`filledBuyerAssets + filledBuyerAssets * referralFeePct / (WAD - referralFeePct)`), but the contract never validates that `maxBuyerAssets` is large enough to cover both components. [6](#0-5) 

The sibling function `buyWithAssetsTargetAndWithdrawCollateral` computes the referral fee upfront and subtracts it from the fill target, leaving the fee in the bundler's balance: [7](#0-6) 

Similarly, `supplyCollateralAndSellWithAssetsTarget` adds the referral fee to the fill target upfront: [8](#0-7) 

**Existing guards:** Only `referralFeePct < WAD` (line 61) and taker authorization (line 60) are checked. Neither prevents the scenario where `maxBuyerAssets` is set tightly to `filledBuyerAssets`. [9](#0-8) 

## Impact Explanation
All offers are successfully taken (`filledUnits == targetUnits` passes at line 88) and collateral withdrawals complete, but the transaction reverts at the referral-fee transfer (line 103), rolling back all state changes. The user loses gas and cannot complete an economically valid fill in a single call. Because the revert is deterministic for the given `(maxBuyerAssets, referralFeePct)` pair, the user is blocked until they either increase `maxBuyerAssets` to cover the referral fee or set `referralFeePct = 0`. This constitutes a denial-of-fill for any user who follows the common tight-slippage pattern.

## Likelihood Explanation
Any unprivileged taker can trigger this by setting `maxBuyerAssets` equal to the expected fill cost (computed off-chain from offer prices) and passing any `referralFeePct > 0`. No special state, privileged role, or external dependency is required. The precondition is trivially reachable and repeatable across every call with these parameters. Tight slippage is a standard integration pattern, making accidental triggering highly probable for integrators who do not account for the referral fee in `maxBuyerAssets`.

## Recommendation
Mirror the pattern used by `buyWithAssetsTargetAndWithdrawCollateral`: compute `referralFeeAssets` upfront from `maxBuyerAssets` and reduce the effective fill budget accordingly, so the referral fee is always reserved before the take loop runs. Alternatively, add an explicit check after the loop: `require(maxBuyerAssets >= filledBuyerAssets + referralFeeAssets, InsufficientBudget())` and revert with a clear error rather than letting the ERC20 transfer fail opaquely.

## Proof of Concept
1. Deploy `MidnightBundles` against a live or forked Midnight instance.
2. Create a sell offer for N units at price P (so `filledBuyerAssets = N * P`).
3. Call `buyWithUnitsTargetAndWithdrawCollateral` with `targetUnits = N`, `maxBuyerAssets = N * P` (exact fill cost, zero slippage), and `referralFeePct = 1e16` (1%).
4. Observe: the take loop succeeds, `filledUnits == targetUnits`, but the transaction reverts at line 103 because the bundler holds 0 tokens when attempting to transfer `referralFeeAssets > 0`.
5. Confirm: calling with `maxBuyerAssets = N * P + referralFeeAssets` succeeds, proving the root cause is the missing budget reservation.

### Citations

**File:** src/periphery/MidnightBundles.sol (L47-48)
```text
    /// @dev Total loan assets transferred from msg.sender is
    /// filledBuyerAssets + filledBuyerAssets * referralFeePct / (WAD - referralFeePct).
```

**File:** src/periphery/MidnightBundles.sol (L60-61)
```text
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
        require(referralFeePct < WAD, PctExceeded());
```

**File:** src/periphery/MidnightBundles.sol (L66-67)
```text
        pullToken(loanToken, msg.sender, maxBuyerAssets, loanTokenPermit);
        forceApproveMax(loanToken, MIDNIGHT);
```

**File:** src/periphery/MidnightBundles.sol (L79-85)
```text
            try IMidnight(MIDNIGHT)
                .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (
                uint256 resBuyerAssets, uint256
            ) {
                filledUnits += unitsToTake;
                filledBuyerAssets += resBuyerAssets;
            } catch {}
```

**File:** src/periphery/MidnightBundles.sol (L102-102)
```text
        uint256 referralFeeAssets = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
```

**File:** src/periphery/MidnightBundles.sol (L103-103)
```text
        if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
```

**File:** src/periphery/MidnightBundles.sol (L104-104)
```text
        SafeTransferLib.safeTransfer(loanToken, msg.sender, maxBuyerAssets - filledBuyerAssets - referralFeeAssets);
```

**File:** src/periphery/MidnightBundles.sol (L200-201)
```text
        uint256 referralFeeAssets = targetBuyerAssets.mulDivDown(referralFeePct, WAD);
        uint256 targetFilledBuyerAssets = targetBuyerAssets - referralFeeAssets;
```

**File:** src/periphery/MidnightBundles.sol (L277-278)
```text
        uint256 referralFeeAssets = targetSellerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
        uint256 targetFilledSellerAssets = targetSellerAssets + referralFeeAssets;
```
