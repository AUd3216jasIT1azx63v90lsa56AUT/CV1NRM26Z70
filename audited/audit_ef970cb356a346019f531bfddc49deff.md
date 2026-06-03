Audit Report

## Title
Fee-on-transfer loanToken causes `repayAndWithdrawCollateral` to always revert - (File: src/periphery/MidnightBundles.sol)

## Summary
`MidnightBundles.repayAndWithdrawCollateral` computes `units = assets - referralFeeAssets` and then calls `pullToken(loanToken, msg.sender, assets, ...)`, which with a fee-on-transfer token delivers only `assets*(1-fee_rate)` to MidnightBundles. The subsequent `Midnight.repay(market, units, ...)` attempts to pull the full `units` from MidnightBundles, which holds fewer tokens than required, causing an unconditional revert. No fee-on-transfer exclusion exists in `SECURITY.md`, `RESEARCHER.md`, or `Midnight.sol`.

## Finding Description
**Verified code path:**

1. `repayAndWithdrawCollateral` computes `units = assets - referralFeeAssets` and calls `pullToken(loanToken, msg.sender, assets, loanTokenPermit)`: [1](#0-0) 

2. `pullToken` fallback path calls `SafeTransferLib.safeTransferFrom(token, from, address(this), amount)`, which succeeds but delivers only `assets*(1-fee_rate)` to MidnightBundles with a fee-on-transfer token: [2](#0-1) 

3. `Midnight.repay` resolves `payer = msg.sender = MidnightBundles` because `callback == address(0)`: [3](#0-2) 

4. `Midnight.repay` then attempts to pull the full `units` from MidnightBundles via `safeTransferFrom`. MidnightBundles holds only `assets*(1-fee_rate)` which is less than `units` whenever `fee_rate > referralFeePct/WAD` (including the common case `referralFeePct = 0`), causing an unconditional revert: [4](#0-3) 

**Root cause:** No balance-delta accounting after `pullToken`. The function assumes `pullToken(assets)` delivers exactly `assets` to MidnightBundles, which is false for fee-on-transfer tokens.

**Why existing checks fail:** The only guards are `require(onBehalf == msg.sender || ...)` and `require(referralFeePct < WAD)`: [5](#0-4) 
Neither validates the actual received balance against `units`.

**Token safety note:** The `MidnightBundles.sol` header states "Inherits the token safety requirements of Midnight (see Midnight.sol)" at line 23, but no fee-on-transfer exclusion exists in `Midnight.sol`, `SECURITY.md`, or `RESEARCHER.md`: [6](#0-5) 

## Impact Explanation
`repayAndWithdrawCollateral` is permanently and deterministically broken for any market whose `loanToken` is fee-on-transfer. Every call reverts at the `safeTransferFrom` inside `Midnight.repay`. Borrowers cannot use the bundler to atomically repay debt and withdraw collateral. No funds are permanently lost since direct `Midnight.repay` remains available, but the bundler's atomic repay-and-withdraw path is fully DoS-ed for this token class. This matches the impact class in `RESEARCHER.md`: "Service unavailability or severe degradation under realistic attacker input." [7](#0-6) 

## Likelihood Explanation
Market creation in Midnight is permissionless, so any user can create a market with a fee-on-transfer loanToken. The bug is deterministic — it triggers on every call, not probabilistically. Preconditions are: (1) market's `loanToken` is fee-on-transfer, (2) borrower has outstanding debt, (3) borrower calls `repayAndWithdrawCollateral`. No special permissions are required beyond having debt and having approved MidnightBundles. Repeatability is 100%.

## Recommendation
After `pullToken`, measure the actual received balance using a before/after balance check and use that as the basis for `units`:

```solidity
uint256 balanceBefore = IERC20(loanToken).balanceOf(address(this));
pullToken(loanToken, msg.sender, assets, loanTokenPermit);
uint256 received = IERC20(loanToken).balanceOf(address(this)) - balanceBefore;
uint256 referralFeeAssets = received.mulDivDown(referralFeePct, WAD);
uint256 units = received - referralFeeAssets;
```

Alternatively, explicitly document and enforce that fee-on-transfer tokens are unsupported as loanTokens in markets used with MidnightBundles.

## Proof of Concept
1. Deploy a fee-on-transfer ERC20 token (e.g., 1% fee on every transfer).
2. Create a Midnight market with this token as `loanToken`.
3. Borrower supplies collateral, takes a borrow position (debt > 0).
4. Borrower approves MidnightBundles for `assets` tokens.
5. Borrower calls `repayAndWithdrawCollateral` with `referralFeePct = 0`, `assets = debt`.
6. `pullToken` delivers `assets * 0.99` to MidnightBundles; `units = assets`.
7. `Midnight.repay` attempts `safeTransferFrom(loanToken, MidnightBundles, Midnight, assets)` — MidnightBundles only holds `assets * 0.99`, revert is guaranteed.

Fork test plan: use Foundry with a mock fee-on-transfer token, assert that step 7 reverts with an ERC20 insufficient balance error.

### Citations

**File:** src/periphery/MidnightBundles.sol (L23-23)
```text
/// @dev Inherits the token safety requirements of Midnight (see Midnight.sol).
```

**File:** src/periphery/MidnightBundles.sol (L325-326)
```text
        require(onBehalf == msg.sender || IMidnight(MIDNIGHT).isAuthorized(onBehalf, msg.sender), Unauthorized());
        require(referralFeePct < WAD, PctExceeded());
```

**File:** src/periphery/MidnightBundles.sol (L329-334)
```text
        uint256 referralFeeAssets = assets.mulDivDown(referralFeePct, WAD);
        uint256 units = assets - referralFeeAssets;
        pullToken(loanToken, msg.sender, assets, loanTokenPermit);
        forceApproveMax(loanToken, MIDNIGHT);

        IMidnight(MIDNIGHT).repay(market, units, onBehalf, address(0), "");
```

**File:** src/periphery/MidnightBundles.sol (L395-397)
```text
        } else {
            SafeTransferLib.safeTransferFrom(token, from, address(this), amount);
        }
```

**File:** src/Midnight.sol (L511-511)
```text
        address payer = callback != address(0) ? callback : msg.sender;
```

**File:** src/Midnight.sol (L520-520)
```text
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
```

**File:** RESEARCHER.md (L15-15)
```markdown
- Service unavailability or severe degradation under realistic attacker input.
```
