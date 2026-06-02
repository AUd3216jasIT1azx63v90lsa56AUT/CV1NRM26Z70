Audit Report

## Title
Borrower-Deployed Reverting Oracle Permanently Freezes Liquidation via Unconditional Bitmap Oracle Loop - (File: src/Midnight.sol)

## Summary
The `liquidate` function unconditionally calls `IOracle.price()` on every collateral in a borrower's bitmap before performing the health check. Because market creation is permissionless and `supplyCollateral` never calls the oracle, any borrower can activate a collateral slot backed by a reverting oracle, making their position permanently unliquidatable. The protocol's own NatDev comments and a Certora formal proof both confirm this behavior.

## Finding Description

**Root cause:** In `src/Midnight.sol` lines 607–618, the `liquidate` while loop calls `IOracle(_collateralParam.oracle).price()` for every bit set in `_position.collateralBitmap` with no error handling. If any single oracle reverts, the entire transaction reverts before the `NotLiquidatable` check at lines 620–624 is ever reached.

**Why the attacker can reach this state:**

1. `touchMarket` (lines 755–791) is permissionless and validates only collateral token sort order, allowed LLTV tiers, and `maxLif` values. No oracle liveness check exists.

2. `supplyCollateral` (lines 524–546) only sets the bitmap bit and transfers the token — no oracle is called, so a reverting oracle is silently activated.

3. The protocol explicitly documents this gap at lines 34–36:
   > "Liquidation reverts if any of the activated collaterals' oracle reverts (see LIVENESS)."
   > "Note that a borrower can activate a collateral once its oracle is reverting because the oracle is not called in supplyCollateral."

**Exploit flow:**

1. Attacker deploys a contract whose `price()` always reverts (the existing `test/helpers/RevertingOracle.sol` is exactly this — it has a `stopOracle()` function that makes `price()` revert).
2. Attacker calls `touchMarket` with one collateral slot pointing to the reverting oracle. Market creation succeeds.
3. Attacker calls `supplyCollateral` for that slot. Bitmap bit is set; no oracle call occurs.
4. Attacker borrows by taking a sell offer, creating debt.
5. Position becomes unhealthy (e.g., other oracle prices drop).
6. Any liquidator calls `liquidate`. The while loop reaches the reverting oracle slot, calls `price()`, which reverts. The entire `liquidate` call reverts. This is permanent.

**Why existing checks fail:**

- The `liquidatorGate` check (lines 597–600) only gates *who* can liquidate, not whether the oracle loop can revert.
- `NotBorrower` and `InconsistentInput` guards fire before the loop but cannot prevent oracle reversion inside it.
- The Certora rule `oracleRevertCausesLiquidateRevert` (certora/specs/Reverts.spec lines 183–193) formally proves this revert propagation, confirming no mitigation exists in the current code.
- `repay` does not call oracles, so the borrower can voluntarily repay, but a liquidator cannot force repayment.

## Impact Explanation

Any borrower who activates a collateral slot backed by a reverting oracle — achievable unilaterally via permissionless market creation — permanently prevents liquidation of their position. The core protocol invariant that unhealthy positions remain liquidatable is broken. Bad debt accumulates with no mechanism for liquidators to recover it, directly threatening lender solvency. This maps to "Permanent lock, freeze, or unrecoverable corruption of user/project state" per RESEARCHER.md.

## Likelihood Explanation

All preconditions are reachable by any EOA:
- Market creation is permissionless (zero privilege required).
- No minimum collateral amount is enforced for bitmap activation.
- The attacker controls the oracle address at market creation time.

The attack is self-contained (create market → supply → borrow → freeze), repeatable across any number of markets, and permanent once the reverting oracle is activated in the bitmap. The `RevertingOracle` test helper already exists in the repo, confirming the developers modeled this exact scenario.

## Recommendation

Wrap the oracle call inside the `liquidate` while loop in a `try/catch`. On revert, either treat the collateral as having zero value (conservative, allows liquidation to proceed) or skip it for the health check while still allowing seizure of non-reverting collaterals. A secondary mitigation is to add an oracle liveness check (a `staticcall` to `price()`) during `touchMarket` or `supplyCollateral`, though this alone does not prevent an oracle from reverting *after* activation.

Example for the loop:

```solidity
uint256 price;
try IOracle(_collateralParam.oracle).price() returns (uint256 p) {
    price = p;
} catch {
    price = 0; // treat reverting oracle collateral as zero value
}
```

## Proof of Concept

Using the existing `test/helpers/RevertingOracle.sol`:

1. Deploy `RevertingOracle`.
2. Call `touchMarket` with a `CollateralParams` array where one slot uses the `RevertingOracle` address.
3. Call `supplyCollateral` for that slot with `assets > 0`. Confirm bitmap bit is set.
4. Call `stopOracle()` on the `RevertingOracle` to make `price()` revert.
5. Borrow by taking a sell offer.
6. Manipulate other oracle prices to make the position unhealthy.
7. Call `liquidate` as a third-party liquidator. Assert the call reverts.
8. Confirm `position.debt > 0` and `maxDebt < debt` (position is unhealthy but unliquidatable).