Audit Report

## Title
Unchecked `uint256` multiplication overflow in `mulDivDown` causes permanent collateral freeze via `isHealthy()` — (`src/Midnight.sol`, `src/libraries/UtilsLib.sol`)

## Summary
`UtilsLib.mulDivDown` computes `(x * y) / d` using Solidity 0.8.x checked arithmetic with no overflow guard. Inside `isHealthy()`, the expression `_position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)` multiplies a `uint128` collateral value by an unconstrained `uint256` oracle price; when their product exceeds `type(uint256).max`, the checked multiplication reverts. Because `withdrawCollateral()` calls `isHealthy()` unconditionally when the borrower has debt, and `take()` does the same for the seller, both entry points permanently revert for any borrower whose stored collateral and live oracle price satisfy the overflow condition, freezing their collateral in the contract.

## Finding Description

**Root cause — `UtilsLib.mulDivDown`:** [1](#0-0) 

The multiplication `x * y` is plain checked arithmetic. No `unchecked` block, no `mulmod`-based overflow guard, and no pre-multiplication bound check exist.

**Overflow site — `isHealthy()`:** [2](#0-1) 

`_position.collateral[i]` is `uint128` (max ≈ 3.4 × 10³⁸). `price` is the raw return value of `IOracle.price()` — an unconstrained `uint256`. The product overflows when:

```
price > type(uint256).max / collateral[i]
      ≈ 2^256 / 2^128 = 2^128 ≈ 3.4 × 10³⁸
```

With `ORACLE_PRICE_SCALE = 1e36`, this corresponds to a real exchange rate of ≈ 340 loan tokens per collateral unit at maximum collateral — reachable for mismatched-decimal pairs or tokens that appreciate after deposit.

**Propagation to `withdrawCollateral()`:** [3](#0-2) 

The `require(isHealthy(...))` call is unconditional when the borrower has debt. If `isHealthy` reverts (rather than returning `false`), the `require` never evaluates — the entire transaction reverts. The borrower cannot withdraw collateral even if their position is economically healthy.

**`take()` is similarly affected**, as confirmed by the protocol's own NatSpec: [4](#0-3) 

**Why the Certora proof does not prevent this on-chain:**

The `NoMultiplicationOverflow.spec` proof is conditioned on a `boundedPrice` ghost assumption: [5](#0-4) 

This is a formal verification assumption (`require`), not an on-chain `require`. No `supplyCollateral`, `touchMarket`, or oracle-wrapper enforces this bound at runtime. The spec comment explicitly labels it an "Oracle integration assumption."

**Exploit flow:**
1. Borrower calls `supplyCollateral` with `assets` near `type(uint128).max` (permitted — `toUint128` only reverts above `type(uint128).max`).
2. Borrower calls `take` to acquire debt.
3. Oracle price rises (or was always high) such that `collateral[i] * price > type(uint256).max`.
4. Any call to `withdrawCollateral` or `take` (when seller has debt) internally calls `isHealthy` → `mulDivDown(collateral, price, ORACLE_PRICE_SCALE)` → checked-arithmetic revert.
5. Borrower's collateral is permanently frozen.

## Impact Explanation
The borrower's collateral tokens are permanently inaccessible: `withdrawCollateral` reverts before the `safeTransfer` at line 572, and `take` reverts before any settlement. The borrower cannot recover funds even if their position is economically solvent. This is a direct, concrete fund freeze scoped to the stated impact.

## Likelihood Explanation
The condition requires `collateral[i] * price > 2^256`. At `collateral[i] = type(uint128).max`, the threshold oracle price is ≈ 340 × `ORACLE_PRICE_SCALE` — achievable for low-decimal collateral tokens paired with high-decimal loan tokens, or any token that appreciates significantly post-deposit. The borrower does not control the oracle, but the condition is reachable through normal market appreciation after a large collateral deposit. The protocol's own NatSpec acknowledges the revert as a known liveness limitation without providing an on-chain mitigation.

## Recommendation
Replace the plain multiplication in `mulDivDown` and `mulDivUp` with an overflow-safe implementation using Solidity's `mulmod` or a 512-bit full-precision multiply-then-divide (e.g., Uniswap v3's `FullMath.mulDiv`). Alternatively, add an on-chain oracle price cap in `isHealthy` (e.g., `require(price <= type(uint256).max / collateral[i])`) before calling `mulDivDown`. The Certora `boundedPrice` assumption should be promoted to an on-chain invariant enforced at the point of oracle consumption.

## Proof of Concept
```solidity
// Minimal forge test sketch
function test_collateralFreeze() public {
    uint128 largeCollateral = type(uint128).max;
    // 1. Supply max collateral
    supplyCollateral(market, largeCollateral, borrower, borrower);
    // 2. Acquire debt via take
    take(market, offer, borrower);
    // 3. Set oracle price above overflow threshold
    oracle.setPrice(type(uint256).max / largeCollateral + 1);
    // 4. withdrawCollateral reverts — collateral frozen
    vm.expectRevert(); // arithmetic overflow inside mulDivDown
    withdrawCollateral(market, 1, borrower, borrower);
}
```
The fuzz variant should sweep `(collateral, price)` pairs satisfying `collateral * price > type(uint256).max` and assert that `withdrawCollateral` reverts with an arithmetic panic rather than `UnhealthyBorrower`.

### Citations

**File:** src/libraries/UtilsLib.sol (L28-31)
```text
    /// @dev Returns (x * y) / d rounded down.
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** src/Midnight.sol (L147-149)
```text
/// @dev If an activated collateral oracle returns a price such that the user's collateral quoted in loan token is
/// greater than type(uint128).max, then liquidate, isHealthy, withdrawCollateral when the borrower has debt, and take
/// whenever the seller still has debt could revert.
```

**File:** src/Midnight.sol (L568-568)
```text
        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());
```

**File:** src/Midnight.sol (L944-960)
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
    }
```

**File:** certora/specs/NoMultiplicationOverflow.spec (L45-50)
```text
// Bound every storage collateral (uint128) * oracle price product.
function boundedPrice(address oracle) returns uint256 {
    uint256 price;
    require to_mathint(price) * max_uint128 + ORACLE_PRICE_SCALE() - 1 <= max_uint256, "same as assuming that collateral * price <= uint256 with mulDivUp rounding headroom";
    return price;
}
```
