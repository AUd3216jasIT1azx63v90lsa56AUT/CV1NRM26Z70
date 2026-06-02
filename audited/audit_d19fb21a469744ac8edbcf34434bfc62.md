All code references verified against the actual source. Here is the validation result:

---

Audit Report

## Title
Fee-on-Transfer Collateral Token Inflates `collateral[i]` Causing Undercollateralized Borrowing - (File: src/Midnight.sol)

## Summary
`supplyCollateral` records the caller-supplied `assets` value directly into `_position.collateral[collateralIndex]` before executing the token transfer. When the collateral token charges a transfer fee, the contract receives fewer tokens than recorded, permanently overstating the borrower's collateral balance. `isHealthy` then computes `maxDebt` from the inflated on-chain value, allowing the borrower to take on more debt than the actual collateral can support, creating immediate bad debt.

## Finding Description
**Root cause — `src/Midnight.sol` lines 533 and 545:**

State is committed using the caller-supplied `assets` parameter before the transfer executes:

```solidity
// Line 533 — state written with caller-supplied `assets`
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets);
// ...
// Line 545 — transfer executes AFTER state is committed
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
```

No balance-before/after delta check exists anywhere in `supplyCollateral`. The accounting uses the input parameter, not the actual received amount.

**`isHealthy` — `src/Midnight.sol` lines 954–955:**

```solidity
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
    .mulDivDown(collateralParam.lltv, WAD);
```

`isHealthy` reads `_position.collateral[i]` — the inflated recorded value — to compute the borrower's maximum allowed debt. It has no knowledge of the actual token balance held by the contract.

**Market creation — `touchMarket` (`src/Midnight.sol` lines 762–773):**

`touchMarket` validates only LLTV tier, `maxLif`, and sorted collateral addresses. There is no check that the collateral token is not a fee-on-transfer token. The protocol is fully permissionless.

**`IERC20` interface — `src/interfaces/IERC20.sol` lines 1–11:**

The interface is a minimal standard ERC20 with no mechanism to detect or reject fee-on-transfer behavior.

**Exploit flow:**

1. Attacker deploys a fee-on-transfer ERC20 (e.g., 1% fee) and calls `touchMarket` to create a permissionless market with it as collateral.
2. Attacker calls `supplyCollateral(market, 0, 100e18, attacker)`.
   - `_position.collateral[0]` is set to `100e18`.
   - Contract actually receives `99e18` tokens.
3. Attacker calls `take(...)` as borrower, borrowing at LLTV based on `100e18` collateral.
   - `isHealthy` computes `maxDebt = 100e18 * price / ORACLE_PRICE_SCALE * lltv / WAD`.
   - Actual backing is only `99e18` tokens.
4. Position is immediately undercollateralized by the fee percentage. The attacker has extracted `lltv * fee_amount` in excess loan assets.
5. Repeated calls amplify the discrepancy. On `withdrawCollateral`, the contract attempts to transfer the recorded amount but may lack the balance, causing a revert or draining other users' collateral.

**Existing checks and why they fail:**

- The `isHealthy` check inside `withdrawCollateral` (line 568) uses the same inflated `_position.collateral[i]`, so it does not catch the discrepancy.
- The Certora `supplyCollateralEffects` rule (line 217 of `certora/specs/BalanceEffects.spec`) asserts `collateral == collateralBefore + assets`, which models the buggy behavior — it verifies the input parameter is recorded, not that the actual received amount is recorded.

## Impact Explanation
An attacker can borrow against overstated collateral, creating an immediately undercollateralized position. The excess debt is unrecoverable bad debt socialized to lenders. On `withdrawCollateral`, the contract may attempt to transfer more tokens than it holds, reverting or draining collateral belonging to other borrowers in the same market. This directly violates the core protocol invariant that contract balances cover recorded collateral. Impact is direct theft of lender assets and permanent insolvency of affected markets.

## Likelihood Explanation
The protocol is fully permissionless — any address can create a market with any ERC20 as collateral token via `touchMarket`. Fee-on-transfer tokens are a well-known and widely deployed ERC20 variant. No precondition requires admin action or victim mistakes. The attack is repeatable on every `supplyCollateral` call and scales linearly with the fee rate and supplied amount. An attacker can deploy their own fee-on-transfer token at zero cost.

## Recommendation
Measure the actual received amount using a balance-before/after check in `supplyCollateral`:

```solidity
uint256 balanceBefore = IERC20(collateralToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
uint256 received = IERC20(collateralToken).balanceOf(address(this)) - balanceBefore;
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + received);
```

Alternatively, add a whitelist or validation check in `touchMarket` to reject tokens that exhibit fee-on-transfer behavior, though the balance-delta approach is more robust and consistent with how Morpho Blue handles this class of token.

## Proof of Concept
1. Deploy `FeeToken` — an ERC20 that deducts 1% on every `transferFrom`.
2. Call `touchMarket` with `FeeToken` as the sole collateral token and a valid LLTV.
3. Call `supplyCollateral(market, 0, 100e18, attacker)`.
   - Assert `position[id][attacker].collateral[0] == 100e18`.
   - Assert `FeeToken.balanceOf(address(midnight)) == 99e18`.
4. Call `take(...)` to borrow `lltv * 100e18 * price / ORACLE_PRICE_SCALE` in loan assets.
5. Assert `isHealthy(market, id, attacker) == true` (passes due to inflated collateral).
6. Assert actual collateral backing is only `99e18`, confirming the position is undercollateralized by `lltv * 1e18 * price / ORACLE_PRICE_SCALE` in loan value.
7. Attempt `withdrawCollateral(market, 0, 100e18, attacker, attacker)` — reverts or drains other users' tokens, confirming the balance shortfall.