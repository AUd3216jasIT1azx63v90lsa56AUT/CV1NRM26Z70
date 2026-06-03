Audit Report

## Title
Dust-collateral double `mulDivDown` rounding collapses `maxDebt` to zero, enabling liquidation of economically overcollateralized positions - (File: `src/Midnight.sol`)

## Summary
Two sequential `mulDivDown` calls in `isHealthy` and `liquidate` each apply floor rounding, compounding precision loss when `collateral[i] = 1 wei`. The intermediate result of the first division can be a small integer that the second division floors to zero, yielding `maxDebt = 0` even though the position's true collateral value exceeds its debt. Because `withdrawCollateral` only clears the bitmap bit when `newCollateral == 0`, a borrower can legitimately withdraw to 1 wei of collateral, and a subsequent routine price drop causes the same rounding path to declare the position unhealthy and liquidatable — directly violating the invariant that economically overcollateralized positions cannot be liquidated.

## Finding Description

**Root cause:** Two sequential `mulDivDown` calls each apply floor rounding, compounding precision loss on dust collateral amounts.

`isHealthy` at `src/Midnight.sol:954–955`:
```solidity
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
    .mulDivDown(collateralParam.lltv, WAD);
```

`liquidate` at `src/Midnight.sol:613` uses the identical two-step rounding:
```solidity
maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(_collateralParam.lltv, WAD);
```

`liquidate` at `src/Midnight.sol:620–624` checks `originalDebt > maxDebt`:
```solidity
require(
    !liquidationLocked(id, borrower)
        && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
    NotLiquidatable()
);
```

`withdrawCollateral` at `src/Midnight.sol:564–566` only clears the bitmap bit when `newCollateral == 0`; leaving 1 wei keeps the bit set and the oracle is queried in every subsequent health check:
```solidity
if (newCollateral == 0 && assets > 0) {
    _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
}
```

Constants confirmed — `WAD = 1e18`, `ORACLE_PRICE_SCALE = 1e36`, `LLTV_2 = 0.77e18` (`src/libraries/ConstantsLib.sol:8–9,31`).

**Concrete arithmetic (lltv = `LLTV_2 = 0.77e18`):**

*At withdrawal time — price = 2e36:*
- `mulDivDown(1, 2e36, 1e36) = 2`
- `mulDivDown(2, 0.77e18, 1e18) = floor(1.54) = 1`
- `maxDebt = 1 >= debt = 1` → `isHealthy` returns `true` → `withdrawCollateral` succeeds, leaving 1 wei of collateral with bitmap bit set.

*After oracle price drops to 1.5e36 (~25% drop):*
- `mulDivDown(1, 1.5e36, 1e36) = 1`
- `mulDivDown(1, 0.77e18, 1e18) = floor(0.77) = 0`
- `maxDebt = 0 < debt = 1` → `isHealthy` returns `false`.

*True economic health at 1.5e36:*
- Collateral value = `1 × 1.5e36 / 1e36 = 1.5` loan-token units
- True max debt = `1.5 × 0.77 = 1.155` loan-token units
- Debt = 1 unit → position IS genuinely overcollateralized (1 < 1.155).

**Why existing checks fail:** There is no minimum-collateral floor and no dust guard anywhere in the codebase. `withdrawCollateral` checks `isHealthy` only at withdrawal time (price = 2e36), which passes. The `liquidate` path recomputes `maxDebt` with the same two-step `mulDivDown` and checks `originalDebt > maxDebt` (1 > 0), which passes unconditionally.

## Impact Explanation
A liquidator seizes 1 wei of collateral (worth 1.5 loan-token units at the example price) by repaying 1 unit of debt, netting ~0.5 loan-token units of profit. The borrower loses collateral the protocol's own health formula should have protected. The core invariant — "a position satisfying `collateral_value × lltv > debt` in real arithmetic cannot be liquidated" — is broken by integer rounding on dust collateral. This constitutes direct, unauthorized movement of value from a borrower to a liquidator.

## Likelihood Explanation
No privileged access is required. Any borrower who supplies a large amount and withdraws `amount − 1` reaches the precondition. The required oracle price range corresponds to a ~25–33% price drop for `LLTV_2`, a routine market event. The vulnerability applies to all LLTV tiers except `LLTV_8 = 1e18` (where `lltv = WAD` makes the second `mulDivDown` lossless). It is repeatable across any market and any collateral token whose oracle price can fall into the described range.

## Recommendation
1. **Enforce a minimum collateral amount** in `supplyCollateral` and `withdrawCollateral` (e.g., require `newCollateral == 0 || newCollateral >= MIN_COLLATERAL`) to prevent dust positions from remaining active.
2. **Alternatively, combine the two `mulDivDown` calls** into a single `mulDivDown(collateral, price * lltv, ORACLE_PRICE_SCALE * WAD)` using 512-bit intermediate arithmetic (via `FullMath` or equivalent) to eliminate the intermediate floor.
3. **Clear the bitmap bit** whenever collateral falls below a dust threshold, not only when it reaches exactly zero.

## Proof of Concept
```solidity
// Minimal Foundry test sketch
function test_dustCollateralLiquidation() public {
    // 1. Borrower supplies large collateral, borrows 1 unit of debt
    vm.prank(borrower);
    midnight.supplyCollateral(market, 0, LARGE_AMOUNT, borrower, "");
    vm.prank(borrower);
    midnight.borrow(market, 1, borrower, borrower);

    // 2. Borrower withdraws all but 1 wei — isHealthy passes at price=2e36
    oracle.setPrice(2e36);
    vm.prank(borrower);
    midnight.withdrawCollateral(market, 0, LARGE_AMOUNT - 1, borrower, borrower);
    // collateral[0] == 1 wei, bitmap bit still set

    // 3. Oracle price drops ~25%
    oracle.setPrice(1.5e36);

    // 4. isHealthy now returns false (maxDebt=0 < debt=1)
    assertFalse(midnight.isHealthy(market, id, borrower));

    // 5. Liquidator liquidates — seizes 1 wei collateral worth 1.5 units, repays 1 unit
    vm.prank(liquidator);
    midnight.liquidate(market, borrower, 0, 1, 0, "");
    // Liquidator profits ~0.5 loan-token units; borrower loses overcollateralized position
}
```