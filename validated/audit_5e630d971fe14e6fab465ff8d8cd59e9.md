Audit Report

## Title
Fee-on-Transfer Collateral Token Inflates `collateral[i]` Causing Undercollateralized Borrowing - (File: src/Midnight.sol)

## Summary
`supplyCollateral` records the caller-supplied `assets` value into `_position.collateral[collateralIndex]` before executing the token transfer. When the collateral token charges a transfer fee, the contract receives fewer tokens than recorded, permanently overstating the borrower's collateral balance. `isHealthy` computes `maxDebt` from the inflated on-chain value, allowing the borrower to take on more debt than the actual collateral can support, creating immediate bad debt at the expense of lenders.

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

1. Attacker deploys a fee-on-transfer ERC20 (e.g., 1% fee) and calls `touchMarket` to create a permissionless market with it as collateral and a legitimate loan token (e.g., USDC).
2. Lenders supply USDC to the market, attracted by interest rates.
3. Attacker calls `supplyCollateral(market, 0, 100e18, attacker)`.
   - `_position.collateral[0]` is set to `100e18`.
   - Contract actually receives `99e18` tokens.
4. Attacker calls `take(...)` as borrower, borrowing at LLTV based on `100e18` collateral.
   - `isHealthy` computes `maxDebt = 100e18 * price / ORACLE_PRICE_SCALE * lltv / WAD`.
   - Actual backing is only `99e18` tokens.
5. Position is immediately undercollateralized by the fee percentage. The attacker has extracted `lltv * fee_amount` in excess loan assets.
6. On `withdrawCollateral`, the contract attempts to transfer the recorded amount but may lack the balance, causing a revert or draining other users' collateral.

**Existing checks and why they fail:**

- The `isHealthy` check inside `withdrawCollateral` (line 568) uses the same inflated `_position.collateral[i]`, so it does not catch the discrepancy.

## Impact Explanation
An attacker can borrow against overstated collateral, creating an immediately undercollateralized position. The excess debt is unrecoverable bad debt socialized to lenders. On `withdrawCollateral`, the contract may attempt to transfer more tokens than it holds, reverting or draining collateral belonging to other borrowers in the same market. This directly violates the core protocol invariant that contract balances cover recorded collateral. Impact is direct theft of lender assets and permanent insolvency of affected markets.

## Likelihood Explanation
The protocol is fully permissionless — any address can create a market with any ERC20 as collateral token via `touchMarket`. Fee-on-transfer tokens are a well-known and widely deployed ERC20 variant. No precondition requires admin action or victim mistakes beyond normal lender participation in a market. The attack is repeatable on every `supplyCollateral` call and scales linearly with the fee rate and supplied amount. An attacker can deploy their own fee-on-transfer token at zero cost.

## Recommendation
Record the actual received amount rather than the caller-supplied parameter. Capture the contract's collateral token balance before and after the transfer, and use the delta as the credited amount:

```solidity
uint256 balanceBefore = IERC20(collateralToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
uint256 received = IERC20(collateralToken).balanceOf(address(this)) - balanceBefore;
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + received);
```

Alternatively, document that fee-on-transfer tokens are not supported and add a validation check in `touchMarket` (though on-chain detection of fee-on-transfer behavior is not reliably possible, so the balance-delta approach is preferred).

## Proof of Concept
1. Deploy a fee-on-transfer ERC20 token with a 1% transfer fee.
2. Deploy a mock oracle returning a fixed price.
3. Call `touchMarket` with the fee token as collateral and USDC as loan token.
4. Have a lender supply USDC liquidity.
5. Call `supplyCollateral(market, 0, 100e18, attacker)` — verify `_position.collateral[0] == 100e18` but `IERC20(feeToken).balanceOf(midnight) == 99e18`.
6. Call `take(...)` to borrow `lltv * 100e18 * price / ORACLE_PRICE_SCALE` in USDC.
7. Verify `isHealthy` returns `true` despite actual collateral being only `99e18`.
8. Verify the borrowed USDC exceeds what `99e18` collateral would support at the given LLTV, confirming undercollateralization and bad debt creation.