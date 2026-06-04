### Title
Chained Multiplication-After-Division in `maxDebt` and `seizedAssets` Computations Causes Precision Loss in Liquidation Accounting — (File: src/Midnight.sol)

---

### Summary

The `liquidate` and `isHealthy` functions compute `maxDebt` using two sequential `mulDivDown` calls, introducing compounded rounding loss. The same pattern appears in the `seizedAssets` branch of `liquidate`. This mirrors the GMX M-32 vulnerability class: a division result is used as the input to a subsequent multiplication, amplifying the truncation error.

---

### Finding Description

**Instance 1 — `maxDebt` (chained `mulDivDown`)**

In both `liquidate` and `isHealthy`, the maximum debt a borrower is allowed to carry is computed per-collateral as:

```solidity
// src/Midnight.sol line 613
maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(_collateralParam.lltv, WAD);
``` [1](#0-0) 

The mathematically correct single-step formula is:

```
maxDebt += _collateral * price * lltv / (ORACLE_PRICE_SCALE * WAD)
```

The chained form first computes `_collateral * price / ORACLE_PRICE_SCALE`, which truncates (rounds down) by up to `ORACLE_PRICE_SCALE − 1` in the numerator, losing up to **1 unit** of the intermediate result. That truncated value is then multiplied by `lltv` and divided by `WAD`. Because `lltv ≤ WAD`, the amplification factor is ≤ 1, so the per-collateral error in `maxDebt` is at most **1 loan-token unit**. With `MAX_COLLATERALS_PER_BORROWER = 16` activated collaterals, the cumulative underestimation of `maxDebt` can reach **up to 16 loan-token units**. [2](#0-1) 

The identical pattern appears in `isHealthy`:

```solidity
// src/Midnight.sol line 954-955
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
    .mulDivDown(collateralParam.lltv, WAD);
``` [3](#0-2) 

**Instance 2 — `seizedAssets` (chained `mulDivDown`)**

When a liquidator specifies `repaidUnits > 0`, the collateral to seize is:

```solidity
// src/Midnight.sol line 652
seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
``` [4](#0-3) 

The correct formula is `repaidUnits * lif * ORACLE_PRICE_SCALE / (WAD * liquidatedCollatPrice)`. The intermediate `repaidUnits * lif / WAD` is truncated by up to 1 unit. That error is then amplified by `ORACLE_PRICE_SCALE / liquidatedCollatPrice`. With `ORACLE_PRICE_SCALE = 1e36`, for a collateral token priced at `1e18` (a very cheap token), the amplification is `1e18`, meaning `seizedAssets` can be underestimated by up to `1e18` collateral wei. [5](#0-4) 

**Root cause** — `UtilsLib.mulDivDown` is a plain `(x * y) / d` with no phantom-overflow protection:

```solidity
// src/libraries/UtilsLib.sol line 29-31
function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y) / d;
}
``` [6](#0-5) 

---

### Impact Explanation

**`maxDebt` underestimation:** A borrower whose true `maxDebt` exceeds their `debt` by fewer than 16 loan-token units (i.e., technically healthy) can have their position flagged as liquidatable and their collateral seized. The borrower suffers an unauthorized liquidation.

**`seizedAssets` underestimation:** The liquidator receives fewer collateral assets than the oracle price entitles them to. The borrower retains excess collateral relative to the debt repaid, creating a small but systematic accounting discrepancy.

---

### Likelihood Explanation

Both code paths are reachable by any unprivileged caller. `liquidate` is permissionless (subject only to the optional `liquidatorGate`). The precision loss is deterministic and present on every liquidation call — it only has observable effect when a position is within a few wei of the health boundary, which is a realistic edge case for positions managed by automated bots that maintain near-maximum leverage.

---

### Recommendation

Replace chained `mulDivDown` with a single combined operation to eliminate the intermediate truncation:

```solidity
// maxDebt — single step
maxDebt += _collateral.mulDivDown(price * _collateralParam.lltv, ORACLE_PRICE_SCALE * WAD);

// seizedAssets — single step
seizedAssets = repaidUnits.mulDivDown(lif * ORACLE_PRICE_SCALE, WAD * liquidatedCollatPrice);
```

Note that `price * lltv` and `lif * ORACLE_PRICE_SCALE` must be checked for overflow before use, or a full-precision `mulDiv` (e.g., OpenZeppelin's `Math.mulDiv`) should be used, as recommended in the original GMX fix.

---

### Proof of Concept

**`maxDebt` underestimation — concrete values:**

- `_collateral = 3`, `price = (1e36 / 3) - 1` (just below ⅓ loan token per collateral unit), `lltv = 0.98e18`

Chained computation:
- Step 1: `3 * ((1e36/3) - 1) / 1e36 = (1e36 - 3) / 1e36 = 0` (truncated from ~0.999…)
- Step 2: `0 * 0.98e18 / 1e18 = 0` → `maxDebt = 0`

True value: `3 * ((1e36/3) - 1) * 0.98e18 / (1e36 * 1e18) ≈ 0.98e18 - 3e-18 ≈ 0.98e18`

A borrower with `debt = 0.5e18` and this collateral position has true `maxDebt ≈ 0.98e18 > debt` (healthy), but computed `maxDebt = 0 < debt` (appears liquidatable). Any liquidator can call `liquidate` and seize the collateral.

### Citations

**File:** src/Midnight.sol (L613-613)
```text
            maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(_collateralParam.lltv, WAD);
```

**File:** src/Midnight.sol (L652-652)
```text
                seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
```

**File:** src/Midnight.sol (L954-955)
```text
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
```

**File:** src/libraries/ConstantsLib.sol (L9-9)
```text
uint256 constant ORACLE_PRICE_SCALE = 1e36;
```

**File:** src/libraries/ConstantsLib.sol (L21-21)
```text
uint256 constant MAX_COLLATERALS_PER_BORROWER = 16;
```

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```
