Audit Report

## Title
Double-Rounding in `isHealthy()` Produces False Unhealthy Result, Blocking Valid `take()` - (`src/Midnight.sol`)

## Summary
`isHealthy()` computes `maxDebt` via two sequential `mulDivDown` calls — first dividing by `ORACLE_PRICE_SCALE`, then by `WAD` — introducing a compounded floor truncation that can undercount `maxDebt` by 1 relative to the exact single-step result. When a seller's post-take debt equals exactly the single-step `maxDebt`, the double-rounding causes `isHealthy()` to return `false`, and `take()` reverts with `SellerIsLiquidatable` despite the position being healthy under exact arithmetic.

## Finding Description

**Root cause — `src/Midnight.sol:954-955`:**
```solidity
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
    .mulDivDown(collateralParam.lltv, WAD);
```
`mulDivDown` is `(x * y) / d` (plain integer division, `src/libraries/UtilsLib.sol:29-31`).

The two-step computation is:
1. `intermediate = ⌊collateral × price / ORACLE_PRICE_SCALE⌋`
2. `maxDebt = ⌊intermediate × lltv / WAD⌋`

The exact single-step value is:
`maxDebt_exact = ⌊collateral × price × lltv / (ORACLE_PRICE_SCALE × WAD)⌋`

**Why double-rounding loses 1 unit:**
Let `collateral × price = q₁ × ORACLE_PRICE_SCALE + r₁` (r₁ > 0) and `q₁ × lltv = q₂ × WAD + r₂` (r₂ > 0). The single-step numerator expands to `q₂ × ORACLE_PRICE_SCALE × WAD + r₂ × ORACLE_PRICE_SCALE + r₁ × lltv`. When `r₂ × ORACLE_PRICE_SCALE + r₁ × lltv ≥ ORACLE_PRICE_SCALE × WAD`, the single-step floor is `q₂ + 1` but the double-step gives `q₂`.

**Concrete counterexample** (`lltv = LLTV_7 = 0.98e18`, confirmed allowed by `isLltvAllowed` at `src/libraries/ConstantsLib.sol:40-42`):
- `collateral = 2`, `price = 1e36 − 1`
- Step 1: `⌊2 × (1e36−1) / 1e36⌋ = 1`
- Step 2: `⌊1 × 0.98e18 / 1e18⌋ = 0`
- Exact: `⌊2 × (1e36−1) × 0.98e18 / (1e36 × 1e18)⌋ = 1`

With `debt = 1`: `maxDebt = 0 < 1 = debt` → `isHealthy()` returns `false`.

**Exploit flow:**
1. Seller supplies `collateral = 2` wei of collateral token via `supplyCollateral`.
2. Oracle is configured to return `price = 1e36 − 1` (observable on-chain).
3. Seller calls `take()` with `units = 1` on a sell offer.
4. `sellerDebtIncrease = 1`; seller's debt becomes 1.
5. `take()` reaches `src/Midnight.sol:476`: `require(liquidationLocked(id, seller) || isHealthy(...), SellerIsLiquidatable())`.
6. `liquidationLocked` is `false` (lock released at line 475 since `wasLocked = false`).
7. `isHealthy()` returns `false` due to double-rounding → revert with `SellerIsLiquidatable`.

**Why existing checks do not stop it:**
The `liquidationLocked` bypass at line 475-476 only helps during callbacks. The `CannotIncreaseDebtPostMaturity` check at line 391 is unrelated. There is no tolerance or single-step fallback in `isHealthy()`. The Certora `Healthiness.spec` proofs (`certora/specs/Healthiness.spec:23`) replace `mulDivDown` with `summaryMulDivDown`, a ghost function with only monotonicity and zero axioms — they do not model the concrete two-step rounding loss.

## Impact Explanation
A seller whose position is exactly at the health boundary after a take — healthy by exact arithmetic — has their `take()` call reverted with `SellerIsLiquidatable`. The seller is denied the maximum borrowing capacity their collateral entitles them to. With up to `MAX_COLLATERALS_PER_BORROWER = 16` activated collaterals, the rounding loss accumulates to up to 16 wei, widening the gap between the exact boundary and what `isHealthy()` accepts. This is a functional DoS on a valid protocol operation that breaks the protocol's stated invariant that healthy positions are not blocked.

## Likelihood Explanation
Requires the seller's post-take debt to equal exactly `maxDebt_exact` (the single-step floor). This is a boundary condition that can be engineered: the seller controls collateral amount and `units` in `take()`, and the oracle price is observable on-chain. With small token amounts (wei-scale) or tokens with few decimals the boundary is easy to hit. The condition is repeatable across any market with any allowed LLTV tier. No privileged access is required.

## Recommendation
Replace the two-step `mulDivDown` chain with a single combined multiplication before dividing:

```solidity
maxDebt += _position.collateral[i].mulDivDown(
    price * collateralParam.lltv,
    ORACLE_PRICE_SCALE * WAD
);
```

This computes `⌊collateral × price × lltv / (ORACLE_PRICE_SCALE × WAD)⌋` in one step, eliminating the intermediate truncation. Overflow safety should be verified: `price` is bounded by oracle design and `collateralParam.lltv ≤ WAD = 1e18`, so `price × lltv` fits in uint256 for any realistic oracle price up to ~1e54 / 1e18 = 1e36, which is exactly `ORACLE_PRICE_SCALE` — the maximum meaningful price. If overflow is a concern, use a `mulDiv` with a 512-bit intermediate (e.g., Solmate's `FullMath`).

## Proof of Concept
Minimal Foundry unit test:

```solidity
// Setup: market with lltv = 0.98e18, oracle returning price = 1e36 - 1
// Seller: supplyCollateral(2 wei)
// Seller: take(units=1) on their own sell offer
// Expected: revert SellerIsLiquidatable
// Actual exact maxDebt: floor(2 * (1e36-1) * 0.98e18 / (1e36 * 1e18)) = 1
// Computed maxDebt (two-step): floor(floor(2*(1e36-1)/1e36) * 0.98e18 / 1e18)
//                             = floor(1 * 0.98e18 / 1e18) = 0
// 0 < 1 = debt → isHealthy returns false → revert
```

A fuzz test targeting `isHealthy()` with `collateral ∈ [1, 10]`, `price ∈ [1e35, 1e36]`, and all allowed LLTV tiers will reliably find inputs where the two-step result is strictly less than the single-step result.