Looking at the actual code at `src/periphery/MidnightBundles.sol` lines 329–334, the described code path is accurate. Let me verify the key mechanics and scope before rendering a verdict.

**Code verification:** [1](#0-0) 

The sequence is exactly as claimed:
1. `units = assets - referralFeeAssets` — derived from nominal `assets`
2. `pullToken(loanToken, msg.sender, assets, ...)` — bundler receives `assets - fee` for fee-on-transfer tokens
3. `IMidnight(MIDNIGHT).repay(market, units, ...)` — attempts to pull `units` from bundler

**Scope check:** `live_context.json` explicitly lists under `core_invariants.external_calls`: *"fee-on-transfer, rebasing, false-return, ERC777-like hooks, and non-standard decimals should be tested if not explicitly excluded"* [2](#0-1) 

No explicit exclusion of fee-on-transfer tokens was found in `SECURITY.md` or `RESEARCHER.md`. The `MidnightBundles.sol` header states it "Inherits the token safety requirements of Midnight," but no exclusion of fee-on-transfer tokens was found in scope documents. [3](#0-2) 

The claim is technically sound and the exploit path is fully reachable by an unprivileged borrower.

---

Audit Report

## Title
Fee-on-transfer loanToken causes `repayAndWithdrawCollateral` to always revert due to bundler balance shortfall - (File: src/periphery/MidnightBundles.sol)

## Summary
`repayAndWithdrawCollateral` computes `units = assets - referralFeeAssets` from the caller-supplied nominal `assets` value, then pulls `assets` from `msg.sender` via `pullToken`. When `loanToken` is a fee-on-transfer token, the bundler receives only `assets - fee`, but subsequently instructs Midnight to pull the full `units` from the bundler. Because `units > bundler_balance` whenever `referralFeeAssets < fee`, the `safeTransferFrom` inside `Midnight.repay` reverts, permanently blocking repayment and collateral withdrawal through this bundler for any fee-on-transfer loan token market.

## Finding Description
**Exact code path (`src/periphery/MidnightBundles.sol` lines 329–334):**

```solidity
uint256 referralFeeAssets = assets.mulDivDown(referralFeePct, WAD);
uint256 units = assets - referralFeeAssets;          // derived from nominal assets
pullToken(loanToken, msg.sender, assets, loanTokenPermit); // bundler receives assets - fee
forceApproveMax(loanToken, MIDNIGHT);
IMidnight(MIDNIGHT).repay(market, units, onBehalf, address(0), ""); // pulls units from bundler
```

**Root cause:** `units` is computed from the nominal `assets` argument before the transfer occurs. There is no balance snapshot (balance-before / balance-after) to determine the actual amount received by the bundler. When `loanToken` charges a transfer fee, the bundler's actual balance after `pullToken` is `assets - fee`, which is less than `units = assets - referralFeeAssets` whenever `referralFeeAssets < fee`.

**Exploit flow:**
1. Borrower calls `repayAndWithdrawCollateral` with a fee-on-transfer `loanToken`, `referralFeePct = 0`, and `assets = D` (their debt).
2. `units = D - 0 = D`.
3. `pullToken` transfers `D` from borrower; bundler receives `D - fee`.
4. `forceApproveMax` grants Midnight unlimited allowance (not the bottleneck).
5. `Midnight.repay(market, D, ...)` calls `safeTransferFrom(loanToken, bundler, midnight, D)`.
6. Bundler balance is `D - fee < D` → ERC20 `transferFrom` reverts → entire call reverts.

**Existing checks reviewed:**
- `require(referralFeePct < WAD)` — does not help; bug exists at `referralFeePct = 0`.
- `forceApproveMax` — approval is not the bottleneck; actual token balance is.
- No balance-before/after check exists anywhere in the function.
- No fee-on-transfer guard or slippage parameter exists.

## Impact Explanation
Any borrower using `repayAndWithdrawCollateral` with a fee-on-transfer loan token (and `referralFeePct` small enough that `referralFeeAssets < transferFee`) will have their repayment permanently blocked through this bundler. The call always reverts at `Midnight.repay`, meaning the borrower cannot reduce their debt or withdraw collateral via this bundler path. The bundler is rendered non-functional for that token type. Note: borrowers retain the ability to repay directly through `Midnight.repay` bypassing the bundler, which limits the severity to a permanent denial-of-service on the bundler's `repayAndWithdrawCollateral` function rather than a complete fund freeze.

## Likelihood Explanation
Fee-on-transfer tokens are a well-established ERC20 pattern. Any market created with such a token as `loanToken` triggers this bug on every `repayAndWithdrawCollateral` call with `referralFeePct = 0`. The precondition is entirely attacker-reachable: the borrower controls `assets`, `referralFeePct`, and market selection. No privileged access is required. The bug is 100% repeatable for any fee-on-transfer loanToken market.

## Recommendation
Replace the nominal-`assets`-based `units` computation with an actual balance delta measurement:

```solidity
uint256 balanceBefore = IERC20(loanToken).balanceOf(address(this));
pullToken(loanToken, msg.sender, assets, loanTokenPermit);
uint256 received = IERC20(loanToken).balanceOf(address(this)) - balanceBefore;

uint256 referralFeeAssets = received.mulDivDown(referralFeePct, WAD);
uint256 units = received - referralFeeAssets;

forceApproveMax(loanToken, MIDNIGHT);
IMidnight(MIDNIGHT).repay(market, units, onBehalf, address(0), "");

if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
```

This ensures `units` is always derived from the actual received amount, making the function safe for fee-on-transfer tokens.

## Proof of Concept
**Minimal Foundry test plan:**
1. Deploy a mock ERC20 with a 1% transfer fee.
2. Create a Midnight market with this token as `loanToken`.
3. Have a borrower take a loan of `D` units.
4. Call `repayAndWithdrawCollateral(market, D, borrower, ..., 0, address(0))` with `referralFeePct = 0`.
5. Observe the call reverts at `Midnight.repay` because the bundler holds `D * 0.99` but attempts to transfer `D`.
6. Confirm the borrower's debt is unchanged and collateral is locked in the bundler path.

### Citations

**File:** src/periphery/MidnightBundles.sol (L23-23)
```text
/// @dev Inherits the token safety requirements of Midnight (see Midnight.sol).
```

**File:** src/periphery/MidnightBundles.sol (L329-334)
```text
        uint256 referralFeeAssets = assets.mulDivDown(referralFeePct, WAD);
        uint256 units = assets - referralFeeAssets;
        pullToken(loanToken, msg.sender, assets, loanTokenPermit);
        forceApproveMax(loanToken, MIDNIGHT);

        IMidnight(MIDNIGHT).repay(market, units, onBehalf, address(0), "");
```

**File:** live_context.json (L231-234)
```json
      "callbacks must not corrupt partial state through reentrancy",
      "ERC20 transfer deltas must match accounting deltas",
      "fee-on-transfer, rebasing, false-return, ERC777-like hooks, and non-standard decimals should be tested if not explicitly excluded",
      "multicall must not bypass per-action invariants"
```
