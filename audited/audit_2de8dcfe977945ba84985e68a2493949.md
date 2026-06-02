Audit Report

## Title
Unchecked oracle price overflow in `liquidate` permanently DoS-es liquidation - (File: src/Midnight.sol)

## Summary
`UtilsLib.mulDivDown` and `mulDivUp` use plain Solidity 0.8.x checked multiplication (`x * y`), which reverts on overflow. Because `touchMarket` imposes no upper bound on oracle return values, an attacker can deploy a malicious oracle returning `type(uint256).max`, create a permissionless market with it, and cause every call to `liquidate` to revert permanently via arithmetic overflow in the `maxDebt`/`badDebt` loop. Lenders who supply credit to such a market cannot recover funds through liquidation.

## Finding Description

**Root cause — `mulDivDown`/`mulDivUp` revert on overflow:**

`src/libraries/UtilsLib.sol` lines 29–36 implement both functions as plain checked arithmetic:

```solidity
function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y) / d;
}
function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y + (d - 1)) / d;
}
```

Under Solidity 0.8.x, `x * y` reverts if the product exceeds `type(uint256).max`.

**Overflow site in `liquidate`:**

`src/Midnight.sol` lines 607–618 iterate over every activated collateral slot and call:

```solidity
maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE)...
badDebt = badDebt.zeroFloorSub(
    _collateral.mulDivUp(price, ORACLE_PRICE_SCALE)...
);
```

`_collateral` is a `uint128` (max ≈ 3.4 × 10³⁸). `ORACLE_PRICE_SCALE = 1e36`. With `price = type(uint256).max` and any non-zero `_collateral`, the product `_collateral * price` overflows `uint256`, reverting the entire transaction. The same overflow hits `mulDivUp` on the `badDebt` path.

**No oracle price validation in `touchMarket`:**

`src/Midnight.sol` lines 755–791 validate only `lltv`, `maxLif`, token ordering, and maturity. The oracle address is accepted without any price-range check. Any address — including a contract returning `type(uint256).max` — is accepted.

**Formal verification treats bounded prices as an assumption, not an invariant:**

`certora/specs/NoMultiplicationOverflow.spec` lines 46–49 explicitly `require` the price bound as a precondition:

```
require to_mathint(price) * max_uint128 + ORACLE_PRICE_SCALE() - 1 <= max_uint256
```

This confirms the protocol has no on-chain enforcement of this bound; it is an integration assumption only.

**Exploit flow:**
1. Attacker deploys `MaliciousOracle` that initially returns a normal price, then can be switched to `type(uint256).max`.
2. Attacker calls `touchMarket` with `collateralParams[0].oracle = address(MaliciousOracle)` — succeeds, no price check.
3. Lenders supply credit to the market (oracle appears legitimate at creation time).
4. Borrowers supply collateral and borrow, creating positions with `_collateral > 0`.
5. Attacker switches oracle to return `type(uint256).max`.
6. Every call to `liquidate` enters the `while (_collateralBitmap != 0)` loop, executes `_collateral * type(uint256).max`, overflows, and reverts.
7. Liquidation is permanently blocked for the entire market.

## Impact Explanation

Liquidation is permanently DoS-ed for any market whose oracle returns a price large enough to overflow `uint256` when multiplied by any non-zero collateral balance. Lenders who supplied credit to such a market cannot recover funds via liquidation. Unhealthy positions cannot be closed, violating the core protocol invariant that unhealthy positions remain liquidatable. This constitutes a permanent freeze of lender funds — a critical in-scope impact.

## Likelihood Explanation

Market creation is fully permissionless — no governance approval, no oracle whitelist. The attacker needs only to deploy a two-line oracle contract and call `touchMarket`. The social-engineering requirement (lenders supplying credit to the malicious market) is feasible because the oracle can initially return a normal price and later switch to `type(uint256).max`, making the market appear legitimate at creation time. The attack is repeatable for any number of markets and requires no privileged access.

## Recommendation

Add an on-chain oracle price bound check. Either:
1. In `touchMarket`, call `IOracle(oracle).price()` and require the returned value satisfies `price * type(uint128).max + ORACLE_PRICE_SCALE - 1 <= type(uint256).max` (matching the Certora assumption).
2. In `liquidate`, use an `unchecked` block with an explicit overflow guard before calling `mulDivDown`/`mulDivUp` with oracle-sourced prices, reverting with a descriptive error if the product would overflow.

Option 1 is preferred as it enforces the invariant at market creation time and aligns with the existing Certora assumption.

## Proof of Concept

```solidity
// MaliciousOracle.sol
contract MaliciousOracle {
    bool public malicious;
    function setMalicious(bool _m) external { malicious = _m; }
    function price() external view returns (uint256) {
        return malicious ? type(uint256).max : 1e36;
    }
}

// Test steps:
// 1. Deploy MaliciousOracle; oracle.price() returns 1e36 (normal).
// 2. Call touchMarket with collateralParams[0].oracle = address(oracle). Succeeds.
// 3. Supply collateral (e.g., 1 wei) and borrow, creating a position with collateral[0] = 1.
// 4. Call oracle.setMalicious(true); oracle.price() now returns type(uint256).max.
// 5. Call liquidate(...). Reverts with arithmetic overflow in mulDivDown at Midnight.sol:613.
// Expected: liquidate always reverts; position is permanently unliquidatable.
```