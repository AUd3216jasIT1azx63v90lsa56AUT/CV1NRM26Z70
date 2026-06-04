### Title
Borrower Can Self-Liquidate to Socialize Bad Debt and Reduce Repayment Obligation — (File: src/Midnight.sol)

### Summary

The `liquidate` function in `Midnight.sol` contains no check preventing `msg.sender` from being the same address as `borrower`. Combined with the protocol's explicit support for zero-input liquidations (both `seizedAssets=0` and `repaidUnits=0`) that realize bad debt with no token transfer, a borrower whose position has bad debt can call `liquidate` on themselves to socialize their bad debt to lenders, then repay only the reduced remaining debt and withdraw their collateral — paying materially less than their actual obligation.

### Finding Description

**Root cause:** The `liquidate` function imposes no `msg.sender != borrower` guard. The only access control is the optional `liquidatorGate`:

```solidity
require(
    market.liquidatorGate == address(0)
        || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
    LiquidatorGatedFromLiquidating()
);
``` [1](#0-0) 

For markets without a `liquidatorGate` (the default — `address(0) = unrestricted`), any address including the borrower can call `liquidate`.

**Bad debt socialization path:** When a position's collateral cannot cover the full debt even at `maxLif`, `badDebt > 0` is computed and immediately subtracted from the borrower's debt, with the loss pushed to lenders via `lossFactor`:

```solidity
if (badDebt > 0) {
    _position.debt -= uint128(badDebt);
    ...
    _marketState.lossFactor = UtilsLib.toUint128(
        type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
    );
    _marketState.totalUnits -= UtilsLib.toUint128(badDebt);
``` [2](#0-1) 

**Zero-input liquidation:** The protocol explicitly supports calling `liquidate` with both inputs zero to realize bad debt with no token transfer:

```solidity
/// @dev Passing both 0 for seizedAssets and repaidUnits allows to realize bad debt with 0 token transferred.
``` [3](#0-2) 

The collateral-seize and debt-repay block is gated on `repaidUnits > 0 || seizedAssets > 0`:

```solidity
if (repaidUnits > 0 || seizedAssets > 0) {
    // seize collateral, repay debt
}
``` [4](#0-3) 

**Exploit flow:**

1. Borrower has `debt = D`, collateral value `C` (at oracle price), `maxLif = L`.
2. Position is unhealthy (`D > C * LLTV`) and has bad debt (`D > C / L`, i.e., `D > C * WAD / maxLif`).
3. Borrower calls `liquidate(seizedAssets=0, repaidUnits=0, borrower=self)`.
   - `badDebt = D − C/L` is computed and subtracted from `_position.debt`.
   - `lossFactor` is updated — lenders absorb `badDebt`.
   - No tokens are transferred.
4. Borrower calls `repay(units = C/L)` — pays only the reduced debt.
5. Borrower calls `withdrawCollateral` — retrieves full collateral `C`.

**Net result:** Borrower pays `C/L` instead of `D`, saving `D − C/L` units at lenders' expense.

**Concrete example** (LLTV = 0.86, `maxLif` ≈ 1.075 using `LIQUIDATION_CURSOR_LOW = 0.25`):
- `debt = 100`, collateral value `= 90`
- `maxDebt = 90 * 0.86 = 77.4` → position is unhealthy
- `badDebt = 100 − 90/1.075 ≈ 16.3`
- Borrower self-liquidates: debt reduced to `83.7`, lenders absorb `16.3`
- Borrower repays `83.7`, withdraws collateral worth `90`
- **Borrower profit: `6.3` units; lender loss: `16.3` units**

Without self-liquidation (normal repay): borrower pays `100`, gets `90` back → net loss of `10`.

### Impact Explanation

**Impact: High.** A borrower with a bad-debt position can unilaterally socialize their shortfall to all lenders in the market via `lossFactor`, reducing their own repayment obligation. Lenders suffer unexpected credit slashing beyond what would occur from a normal external liquidation. The borrower converts a net loss into a net gain. This is a direct, unauthorized transfer of value from lenders to the borrower.

### Likelihood Explanation

**Likelihood: Low-to-Medium.** The precondition is that the borrower's position must have bad debt (`debt > collateral / maxLif`), which requires a meaningful collateral price decline. However:
- This is a realistic scenario during sharp market downturns.
- The borrower has a clear, direct financial incentive to execute this.
- No privileged access is required — only a standard user action on a market without a `liquidatorGate`.
- Markets without a `liquidatorGate` are the default (gate is optional).
- The borrower can monitor their own health factor and execute atomically.

### Recommendation

Add an explicit check in `liquidate` to prevent the borrower from liquidating themselves:

```solidity
require(msg.sender != borrower, SelfLiquidation());
```

Alternatively, if self-liquidation is intentionally permitted, restrict bad-debt realization (the zero-input path) to privileged addresses or external liquidators only, consistent with the `liquidatorGate` mechanism. Markets handling significant TVL should be strongly encouraged to deploy a `liquidatorGate` that blocks the borrower address.

### Proof of Concept

**Preconditions:**
- Market with no `liquidatorGate` (default).
- LLTV = 0.86, `maxLif` ≈ 1.075 (`LIQUIDATION_CURSOR_LOW`).
- Borrower has `debt = 100e18`, collateral value = `90e18` (oracle price drop).

**Steps:**

```
// Step 1: Borrower self-liquidates with zero inputs to realize bad debt
midnight.liquidate(
    market,
    collateralIndex,
    0,           // seizedAssets = 0
    0,           // repaidUnits = 0
    borrower,    // msg.sender == borrower
    false,
    borrower,
    address(0),
    ""
);
// Result: _position.debt reduced by ~16.3e18; lossFactor updated (lenders slashed)

// Step 2: Borrower repays only the reduced debt (~83.7e18 instead of 100e18)
midnight.repay(market, 83.7e18, borrower, address(0), "");

// Step 3: Borrower withdraws full collateral
midnight.withdrawCollateral(market, collateralIndex, 90e18, borrower, borrower);

// Net: Borrower paid 83.7e18, received 90e18 collateral → profit of 6.3e18
// Lenders: absorbed 16.3e18 bad debt via lossFactor
```

**Expected outcome:** Borrower reduces their repayment by `~16.3e18` units, capturing value that should have been their loss, at the direct expense of lenders in the market.

### Citations

**File:** src/Midnight.sol (L577-577)
```text
    /// @dev Passing both 0 for seizedAssets and repaidUnits allows to realize bad debt with 0 token transferred.
```

**File:** src/Midnight.sol (L597-600)
```text
        require(
            market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
            LiquidatorGatedFromLiquidating()
        );
```

**File:** src/Midnight.sol (L626-634)
```text
        if (badDebt > 0) {
            // forge-lint: disable-next-item(unsafe-typecast) as badDebt <= _position.debt
            _position.debt -= uint128(badDebt);
            uint256 _totalUnits = _marketState.totalUnits;
            uint256 _lossFactor = _marketState.lossFactor;
            _marketState.lossFactor = UtilsLib.toUint128(
                type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
            );
            _marketState.totalUnits -= UtilsLib.toUint128(badDebt);
```

**File:** src/Midnight.sol (L643-643)
```text
        if (repaidUnits > 0 || seizedAssets > 0) {
```
