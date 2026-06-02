All referenced code has been verified. Here is the validation result:

Audit Report

## Title
Double-Rounding in `isHealthy()` Produces False Unhealthy Result, Blocking Valid `take()` - (`src/Midnight.sol`)

## Summary
`isHealthy()` computes `maxDebt` via two sequential `mulDivDown` calls — first dividing by `ORACLE_PRICE_SCALE`, then by `WAD` — introducing a compounded floor truncation that can undercount `maxDebt` by 1 relative to the exact single-step result. When a seller's post-take debt equals exactly the single-step `maxDebt`, the double-rounding causes `isHealthy()` to return `false`, and `take()` reverts with `SellerIsLiquidatable` despite the position being healthy under exact arithmetic. No privileged access is required to trigger this condition.

## Finding Description

**Root cause — `src/Midnight.sol:954-955`:**
```solidity
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
    .mulDivDown(collateralParam.lltv, WAD);
```

`mulDivDown` is confirmed as plain integer floor division (`src/libraries/UtilsLib.sol:29-31`):
```solidity
function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y) / d;
}
```

The two-step computation is:
1. `intermediate = ⌊collateral × price / ORACLE_PRICE_SCALE⌋`
2. `maxDebt = ⌊intermediate × lltv / WAD⌋`

The exact single-step value is:
`maxDebt_exact = ⌊collateral × price × lltv / (ORACLE_PRICE_SCALE × WAD)⌋`

**Why double-rounding loses 1 unit:**
Let `collateral × price = q₁ × ORACLE_PRICE_SCALE + r₁` (r₁ > 0) and `q₁ × lltv = q₂ × WAD + r₂` (r₂ > 0). The single-step numerator expands to `q₂ × ORACLE_PRICE_SCALE × WAD + r₂ × ORACLE_PRICE_SCALE + r₁ × lltv`. When `r₂ × ORACLE_PRICE_SCALE + r₁ × lltv ≥ ORACLE_PRICE_SCALE × WAD`, the single-step floor is `q₂ + 1` but the double-step gives `q₂`.

**Concrete counterexample** (`lltv = LLTV_7 = 0.98e18`, confirmed valid by `isLltvAllowed` at `src/libraries/ConstantsLib.sol:40-42`):
- `collateral = 2`, `price = 1e36 − 1`
- Step 1: `⌊2 × (1e36−1) / 1e36⌋ = ⌊(2e36−2)/1e36⌋ = 1`
- Step 2: `⌊1 × 0.98e18 / 1e18⌋ = 0`
- Exact: `⌊2 × (1e36−1) × 0.98e18 / 1e54⌋ = ⌊(1.96e54 − 1.96e18)/1e54⌋ = 1`

With `debt = 1`: `maxDebt = 0 < 1 = debt` → `isHealthy()` returns `false`.

**Exploit flow:**
1. Seller supplies `collateral = 2` wei via `supplyCollateral` (standard unprivileged call, `src/Midnight.sol:524-546`).
2. Oracle is configured to return `price = 1e36 − 1` (observable on-chain).
3. Seller posts a sell offer (`offer.buy = false`).
4. Taker calls `take()` with `units = 1`; seller has no existing credit, so `sellerCreditDecrease = 0`, `sellerDebtIncrease = 1` (`src/Midnight.sol:383-384`).
5. `sellerPos.debt` becomes 1 (`src/Midnight.sol:414`).
6. At `src/Midnight.sol:475`: `wasLocked = false`, so `tExchange` releases the lock.
7. At `src/Midnight.sol:476`: `liquidationLocked` is `false`; `isHealthy()` returns `false` due to double-rounding → revert with `SellerIsLiquidatable`.

**Why existing checks do not stop it:**
- The `liquidationLocked` bypass at line 475-476 only helps during callbacks; it is released before the health check when `wasLocked = false`.
- The `CannotIncreaseDebtPostMaturity` check at line 391 is unrelated.
- There is no tolerance or single-step fallback in `isHealthy()`.
- The Certora `Healthiness.spec` proofs (`certora/specs/Healthiness.spec:23`) replace `mulDivDown` with `summaryMulDivDown`, a ghost function with only monotonicity and zero axioms — they do not model the concrete two-step rounding loss and therefore do not catch this bug.

## Impact Explanation
A seller whose position is exactly at the health boundary after a take — healthy by exact arithmetic — has their `take()` call reverted with `SellerIsLiquidatable`. The seller is denied the maximum borrowing capacity their collateral entitles them to. With up to `MAX_COLLATERALS_PER_BORROWER = 16` activated collaterals (`src/libraries/ConstantsLib.sol:21`), the rounding loss accumulates to up to 16 wei, widening the gap between the exact boundary and what `isHealthy()` accepts. This is a functional DoS on a valid protocol operation that breaks the protocol's stated invariant that healthy positions are not blocked.

## Likelihood Explanation
Requires the seller's post-take debt to equal exactly `maxDebt_exact` (the single-step floor). This is a boundary condition that can be engineered: the seller controls collateral amount and the oracle price is observable on-chain. With small token amounts (wei-scale) or tokens with few decimals the boundary is easy to hit. The condition is repeatable across any market with any allowed LLTV tier. No privileged access is required.

## Recommendation
Replace the two-step `mulDivDown` chain with a single-step full-precision multiplication. Use a 512-bit intermediate `mulDiv` (e.g., Solmate's `FullMath.mulDiv`) to compute:

```solidity
maxDebt += FullMath.mulDiv(
    _position.collateral[i],
    price * collateralParam.lltv,   // or pass as separate factors
    ORACLE_PRICE_SCALE * WAD
);
```

Since `price * lltv` can overflow `uint256` (price up to ~1e36, lltv up to 1e18), a 512-bit intermediate is necessary. Alternatively, restructure as `mulDiv(collateral * price, lltv, ORACLE_PRICE_SCALE * WAD)` using a library that handles the full 512-bit product. This eliminates the intermediate floor truncation and ensures `isHealthy()` matches exact arithmetic.

## Proof of Concept
Minimal Foundry unit test:

```solidity
// Set oracle price to 1e36 - 1
oracle.setPrice(1e36 - 1);

// Supply 2 wei of collateral for seller
deal(collateralToken, seller, 2);
vm.prank(seller);
collateralToken.approve(address(midnight), 2);
vm.prank(seller);
midnight.supplyCollateral(market, 0, 2, seller);

// Seller posts sell offer, taker takes 1 unit
// sellerDebtIncrease = 1, seller.debt = 1
// isHealthy: maxDebt = mulDivDown(2, 1e36-1, 1e36).mulDivDown(0.98e18, 1e18)
//          = 1.mulDivDown(0.98e18, 1e18) = 0
// 0 >= 1 → false → SellerIsLiquidatable revert
vm.expectRevert(IMidnight.SellerIsLiquidatable.selector);
take(1, taker, sellerOffer);

// Verify position is actually healthy by exact math:
// floor(2 * (1e36-1) * 0.98e18 / (1e36 * 1e18)) = floor(1.96 - epsilon) = 1 >= 1 ✓
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** src/Midnight.sol (L383-384)
```text
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
```

**File:** src/Midnight.sol (L414-414)
```text
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/Midnight.sol (L475-476)
```text
        if (!wasLocked) UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, false);
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

**File:** src/Midnight.sol (L944-959)
```text
    function isHealthy(Market memory market, bytes32 id, address borrower) public view returns (bool) {
        Position storage _position = position[id][borrower];
        uint256 debt = _position.debt;
        uint256 maxDebt;
        if (debt > 0) {
            uint128 _collateralBitmap = _position.collateralBitmap;
            while (_collateralBitmap != 0) {
                uint256 i = UtilsLib.msb(_collateralBitmap);
                CollateralParams memory collateralParam = market.collateralParams[i];
                uint256 price = IOracle(collateralParam.oracle).price();
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
                _collateralBitmap = _collateralBitmap.clearBit(i);
            }
        }
        return maxDebt >= debt;
```

**File:** src/libraries/UtilsLib.sol (L28-31)
```text
    /// @dev Returns (x * y) / d rounded down.
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** src/libraries/ConstantsLib.sol (L8-9)
```text
uint256 constant WAD = 1e18;
uint256 constant ORACLE_PRICE_SCALE = 1e36;
```

**File:** src/libraries/ConstantsLib.sol (L36-42)
```text
uint256 constant LLTV_7 = 0.98e18;
uint256 constant LLTV_8 = 1e18;

/// @dev Returns true if lltv is one of the allowed LLTV tiers.
function isLltvAllowed(uint256 lltv) pure returns (bool) {
    return lltv == LLTV_0 || lltv == LLTV_1 || lltv == LLTV_2 || lltv == LLTV_3 || lltv == LLTV_4 || lltv == LLTV_5 || lltv == LLTV_6 || lltv == LLTV_7 || lltv == LLTV_8;
}
```

**File:** certora/specs/Healthiness.spec (L20-24)
```text
    // Summarize mulDivDown and mulDivUp to simplify the verification task.
    // Use a ghost function that ensures mulDivDown/Up behaves deterministically and add only the axioms about mulDiv that are needed to prove the desired property.
    // The axioms are proved in MulDiv.spec.
    function UtilsLib.mulDivDown(uint256 x, uint256 y, uint256 d) internal returns (uint256) => summaryMulDivDown(x, y, d);
    function UtilsLib.mulDivUp(uint256 x, uint256 y, uint256 d) internal returns (uint256) => summaryMulDivUp(x, y, d);
```
