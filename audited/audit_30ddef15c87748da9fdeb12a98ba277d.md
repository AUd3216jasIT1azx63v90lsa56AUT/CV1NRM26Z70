Audit Report

## Title
Fee-on-Transfer Collateral Inflates `position.collateral` Accounting, Enabling Undercollateralized Borrowing - (File: src/Midnight.sol)

## Summary
`supplyCollateral` records the caller-supplied `assets` value into `_position.collateral[collateralIndex]` before executing the ERC20 transfer, with no balance-delta verification. For fee-on-transfer collateral tokens, the protocol records more collateral than it actually receives, allowing a borrower's position to appear healthier than it is. The subsequent `isHealthy` check uses the inflated storage value, permitting undercollateralized debt that cannot be fully recovered at liquidation, creating bad debt socialized among lenders.

## Finding Description
**Root cause — accounting write precedes transfer, no delta check:**

In `supplyCollateral` (`src/Midnight.sol:533`), the storage update unconditionally trusts `assets`:
```solidity
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets);
```
The actual ERC20 transfer occurs at line 545:
```solidity
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
```
`SafeTransferLib.safeTransferFrom` (`src/libraries/SafeTransferLib.sol:24-34`) only checks the boolean return value; it does not snapshot `balanceOf(address(this))` before and after to verify the received delta. For a token with a 1% transfer fee, the protocol receives `assets * 0.99` but records `assets`.

**Health check uses inflated value:**

`isHealthy` (`src/Midnight.sol:954-955`) computes `maxDebt` directly from `_position.collateral[i]`:
```solidity
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
    .mulDivDown(collateralParam.lltv, WAD);
```
No cross-check against actual contract token balance is performed.

**Exploit path via `take` → `onSell` → `supplyCollateral`:**

1. Attacker deploys a fee-on-transfer ERC20 (e.g., 1% fee) and calls `touchMarket` to create a market with it as collateral. `touchMarket` (`src/Midnight.sol:755-791`) validates only LLTV tiers, sorted addresses, and `maxLif` — no token-type restriction.
2. A lender creates a buy offer ratified via `SetterRatifier`. `isRatified` (`src/ratifiers/SetterRatifier.sol:30-37`) only verifies the Merkle proof and root membership — no collateral-token checks.
3. Attacker deploys a callback contract implementing `ISellCallback.onSell` that calls `supplyCollateral(market, index, amount, attacker)`. Attacker calls `setIsAuthorized(callbackContract, true, attacker)` — a standard self-authorization.
4. Attacker calls `take(lenderOffer, ...)` with `sellerCallback = callbackContract`. Inside `take`:
   - `sellerPos.debt += sellerDebtIncrease` is written (`src/Midnight.sol:414`).
   - Loan tokens are transferred to the attacker (`src/Midnight.sol:455-456`).
   - `onSell` is invoked (`src/Midnight.sol:458-473`); inside it, `supplyCollateral` records `amount` but only receives `amount * 0.99`.
   - After the callback, `isHealthy` is called (`src/Midnight.sol:476`) using the inflated `_position.collateral[index] = amount`.
5. Health check passes. The attacker holds `units` of debt backed by only `amount * 0.99` actual collateral tokens while the protocol believes it holds `amount`.

**Existing guards are insufficient:**
- `supplyCollateral` has no balance-before/after guard.
- `SafeTransferLib` only checks the return boolean, not the received delta.
- `isHealthy` reads storage directly without comparing to actual balances.
- `touchMarket` imposes no token-type constraints.
- `SetterRatifier.isRatified` is purely offer-validity logic.

## Impact Explanation
The protocol's core solvency invariant — "contract token balances must cover withdrawable assets, collateral claims, credit redemptions, and accrued fees" — and the `external_calls` invariant — "ERC20 transfer deltas must match accounting deltas" — are both violated immediately upon the first `supplyCollateral` call with a fee-on-transfer token. At liquidation, the seized collateral is less than the recorded amount, leaving unpayable bad debt that is proportionally socialized among lenders. This constitutes direct loss of lender funds and protocol insolvency, both listed as highest-priority bug classes in `live_context.json`.

## Likelihood Explanation
Market creation is fully permissionless; any unprivileged user can deploy a fee-on-transfer ERC20 and create a market with it as collateral. The attacker only needs to authorize their own callback contract, which is a standard self-authorization. `live_context.json` explicitly lists "fee-on-transfer… should be tested if not explicitly excluded" as an in-scope `external_calls` invariant, and no exclusion exists anywhere in the codebase. The exploit is repeatable on every `supplyCollateral` call with a fee-bearing token and requires no privileged access.

## Recommendation
Replace the unconditional accounting write with a balance-delta pattern in `supplyCollateral`:

```solidity
uint256 balanceBefore = IERC20(collateralToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
uint256 received = IERC20(collateralToken).balanceOf(address(this)) - balanceBefore;
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + received);
```

This ensures the recorded collateral always equals the actual received amount, regardless of token fee behavior. Alternatively, explicitly document and enforce that only standard (non-fee-on-transfer, non-rebasing) ERC20 tokens are supported as collateral, and add a validation hook in `touchMarket` or a registry of approved collateral tokens.

## Proof of Concept
**Minimal Foundry test outline:**

1. Deploy a `FeeToken` ERC20 that deducts 1% on every `transferFrom` call.
2. Deploy a `Midnight` instance and call `touchMarket` with `FeeToken` as collateral and a standard ERC20 as loan token.
3. Have a lender supply loan tokens and ratify a buy offer via `SetterRatifier`.
4. Deploy an `AttackerCallback` contract implementing `ISellCallback.onSell` that calls `supplyCollateral(market, 0, 1000e18, attacker)`.
5. Attacker calls `setIsAuthorized(AttackerCallback, true, attacker)`.
6. Attacker calls `take(lenderOffer, ..., AttackerCallback, ...)`.
7. **Assert:** `position[id][attacker].collateral[0] == 1000e18` but `FeeToken.balanceOf(address(midnight)) == 990e18`.
8. **Assert:** `isHealthy(market, id, attacker) == true` despite the 10e18 shortfall.
9. Liquidate the position and observe that seized collateral (`990e18`) is less than the recorded debt backing, confirming bad debt creation.