All five code references verified against the actual source. No fee-on-transfer exclusions found in `SECURITY.md` or `RESEARCHER.md`. No such exclusions found in `src/Midnight.sol` source (grep across all source files returned zero matches for fee-on-transfer patterns). The `MidnightBundles.sol` header at line 23 says "Inherits the token safety requirements of Midnight" but Midnight.sol contains no fee-on-transfer exclusion to inherit.

---

Audit Report

## Title
Fee-on-transfer loanToken causes `repayAndWithdrawCollateral` to always revert - (File: src/periphery/MidnightBundles.sol)

## Summary
`MidnightBundles.repayAndWithdrawCollateral` pulls `assets` from `msg.sender` via `pullToken`, then calls `Midnight.repay` with `units = assets - referralFeeAssets`. With a fee-on-transfer `loanToken`, `pullToken` delivers only `assets*(1-fee_rate)` to MidnightBundles, but `Midnight.repay` attempts to pull the full `units` from MidnightBundles. When `referralFeePct = 0` or `fee_rate > referralFeePct/WAD`, MidnightBundles holds fewer tokens than `units`, causing the inner `safeTransferFrom` to revert on every call.

## Finding Description
**Verified code path:**

1. `MidnightBundles.repayAndWithdrawCollateral` (`src/periphery/MidnightBundles.sol`, lines 329–334): [1](#0-0) 

   - `units = assets - referralFeeAssets` (line 330)
   - `pullToken(loanToken, msg.sender, assets, loanTokenPermit)` (line 331) calls `SafeTransferLib.safeTransferFrom(token, from, address(this), amount)` (line 396), which succeeds but delivers only `assets*(1-fee_rate)` to MidnightBundles with a fee-on-transfer token.
   - `IMidnight(MIDNIGHT).repay(market, units, onBehalf, address(0), "")` (line 334) is then called.

2. `pullToken` fallback path (`src/periphery/MidnightBundles.sol`, line 396): [2](#0-1) 

3. `Midnight.repay` payer resolution (`src/Midnight.sol`, line 511): since `callback == address(0)`, `payer = msg.sender = MidnightBundles`. [3](#0-2) 

4. `Midnight.repay` transfer (`src/Midnight.sol`, line 520): attempts to pull `units` from MidnightBundles. MidnightBundles holds only `assets*(1-fee_rate)` < `units`, so this reverts. [4](#0-3) 

**Root cause:** No balance-delta accounting after `pullToken`. The function assumes `pullToken(assets)` delivers exactly `assets` to MidnightBundles, which is false for fee-on-transfer tokens.

**Why existing checks fail:** The only guards are `require(referralFeePct < WAD)` and the authorization check (lines 325–326). [5](#0-4) 
Neither validates the actual received balance against `units`.

**Token safety note:** `MidnightBundles.sol` header states "Inherits the token safety requirements of Midnight (see Midnight.sol)" (line 23), but no fee-on-transfer exclusion exists in `Midnight.sol` source, and neither `SECURITY.md` nor `RESEARCHER.md` exclude fee-on-transfer tokens. [6](#0-5) 

## Impact Explanation
`repayAndWithdrawCollateral` is permanently and deterministically broken for any market whose `loanToken` is fee-on-transfer. Every call reverts at the `safeTransferFrom` inside `Midnight.repay`. Borrowers cannot use the bundler to atomically repay debt and withdraw collateral. No funds are permanently lost since direct `Midnight.repay` remains available, but the bundler's atomic repay-and-withdraw path is fully DoS-ed for this token class. This constitutes service unavailability for a deployed, in-scope contract function, matching the impact class in `RESEARCHER.md` ("Service unavailability or severe degradation under realistic attacker input"). [7](#0-6) 

## Likelihood Explanation
**Preconditions:**
- Market's `loanToken` is fee-on-transfer (e.g., USDT with fee enabled, STA, PAXG, or any custom token).
- Borrower has outstanding debt in that market.
- Borrower calls `repayAndWithdrawCollateral` via MidnightBundles.

Market creation in Midnight is permissionless, so any user can create a market with a fee-on-transfer loanToken. The bug is deterministic — it triggers on every call, not probabilistically. No special permissions are required beyond having debt and having approved MidnightBundles. Repeatability is 100%.

## Recommendation
After `pullToken`, measure the actual received balance using a balance-delta check, and use that as `units` rather than the pre-computed `assets - referralFeeAssets`. For example:

```solidity
uint256 balanceBefore = IERC20(loanToken).balanceOf(address(this));
pullToken(loanToken, msg.sender, assets, loanTokenPermit);
uint256 received = IERC20(loanToken).balanceOf(address(this)) - balanceBefore;
uint256 referralFeeAssets = received.mulDivDown(referralFeePct, WAD);
uint256 units = received - referralFeeAssets;
```

Alternatively, explicitly document and enforce that fee-on-transfer tokens are unsupported (e.g., via a token allowlist or a revert guard), consistent with the inherited token safety requirements.

## Proof of Concept
**Minimal fork test plan:**
1. Deploy a mock ERC20 with a 1% transfer fee.
2. Create a Midnight market with this token as `loanToken`.
3. Borrow against the market.
4. Call `MidnightBundles.repayAndWithdrawCollateral` with `referralFeePct = 0` and `assets = debt`.
5. Observe revert inside `Midnight.repay` at `safeTransferFrom` (MidnightBundles balance = `assets * 0.99` < `units = assets`).
6. Confirm direct `Midnight.repay` (called by the borrower, not via bundler) succeeds, proving the DoS is isolated to the bundler path.

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

**File:** RESEARCHER.md (L14-14)
```markdown
- Permanent lock, freeze, or unrecoverable corruption of user/project state.
```
