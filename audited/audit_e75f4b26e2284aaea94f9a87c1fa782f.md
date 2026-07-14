### Title
Oracle Zero Price Allows Free Collateral Seizure in `liquidate()` - (File: src/Midnight.sol)

---

### Summary

When the oracle for the liquidated collateral returns `0`, the `seizedAssets`-input path in `liquidate()` computes `repaidUnits = 0`, allowing a liquidator to seize the borrower's entire collateral balance while repaying nothing. The bad debt is simultaneously socialized among lenders, creating a double loss.

---

### Finding Description

**Vulnerability class:** Pricing/rounding — oracle zero-price not guarded in liquidation math.

In `liquidate()`, the oracle price for the liquidated collateral is fetched and stored in `liquidatedCollatPrice`: [1](#0-0) 

When `seizedAssets > 0` is the caller-supplied input, `repaidUnits` is derived as: [2](#0-1) 

`mulDivUp(seizedAssets, 0, ORACLE_PRICE_SCALE)` evaluates to `(seizedAssets * 0 + (1e36 - 1)) / 1e36 = 0` via integer division. So `repaidUnits = 0`. [3](#0-2) 

The function then proceeds to: [4](#0-3) 

The collateral is transferred out, `_position.debt -= 0` (no-op), and `_marketState.withdrawable += 0` (no-op). The liquidator receives real collateral tokens for zero loan token repayment.

**Liquidatability precondition when oracle returns 0:** When the oracle returns `0` for the liquidated collateral, its contribution to `maxDebt` is also `0`: [5](#0-4) 

If this is the borrower's only collateral, `maxDebt = 0`, so `originalDebt > maxDebt` is trivially true for any borrower with debt, making the position liquidatable.

**Bad debt compounding:** Simultaneously, `badDebt` is computed as: [6](#0-5) 

With `price = 0`, `badDebt = originalDebt` (all debt is socialized). Lenders absorb the full debt loss via `lossFactor` update, while the liquidator walks away with the collateral for free. [7](#0-6) 

**The protocol's own formal verification explicitly excludes this case** with an assumption rather than a code-level guard: [8](#0-7) 

The LIVENESS comment only documents the `repaidUnits`-input revert (division by zero), not the `seizedAssets`-input free-seizure path: [9](#0-8) 

---

### Impact Explanation

A liquidator can drain a borrower's entire collateral balance for zero loan token repayment whenever the oracle for that collateral returns `0`. Simultaneously, the borrower's full debt is socialized as bad debt, slashing all lenders in the market via `lossFactor`. This is a direct theft of collateral assets and a forced loss on lenders — both in the same transaction.

---

### Likelihood Explanation

Oracle prices returning `0` is a realistic scenario: Chainlink feeds can return `0` on circuit-breaker activation, stale-price fallback, or feed deprecation. The `IOracle` interface imposes no non-zero constraint: [10](#0-9) 

The market is permissionless — any oracle contract can be registered. A malicious integrator could deploy an oracle that transiently returns `0`. The attacker needs no privileged access; only the ability to call `liquidate()` with `seizedAssets > 0`.

---

### Recommendation

Add an explicit zero-price guard before the `seizedAssets`/`repaidUnits` computation block:

```solidity
require(liquidatedCollatPrice > 0, ZeroLiquidatedCollateralPrice());
```

This mirrors the fix applied in GMX-Synthetics (adding the missing error type to the guard list) — the root cause is identical: a specific oracle-zero-price case is not included in the set of conditions that trigger a protective revert.

---

### Proof of Concept

1. Market created with a single collateral whose oracle is `MockOracle`.
2. Borrower supplies 1000 WETH collateral, borrows 800 USDC (healthy at normal price).
3. `MockOracle.price()` is set to return `0` (simulating a circuit-breaker event).
4. Attacker calls:
   ```solidity
   midnight.liquidate(
       market,
       0,           // collateralIndex
       1000e18,     // seizedAssets = full collateral
       0,           // repaidUnits = 0 (caller input)
       borrower,
       false,
       attacker,
       address(0),
       ""
   );
   ```
5. Inside `liquidate()`:
   - `liquidatedCollatPrice = 0`
   - `maxDebt = 0` → `originalDebt > maxDebt` → liquidatable ✓
   - `badDebt = originalDebt` → full debt socialized, lenders slashed
   - `repaidUnits = mulDivUp(1000e18, 0, 1e36) = 0`
   - `_position.collateral[0] -= 1000e18` → collateral seized
   - `_position.debt -= 0` → debt unchanged (already zeroed by bad debt)
6. Attacker receives 1000 WETH, pays 0 USDC. Lenders absorb 800 USDC bad debt.

### Citations

**File:** src/Midnight.sol (L146-146)
```text
/// @dev If the liquidated collateral oracle returns 0 on price, liquidate with repaid input reverts.
```

**File:** src/Midnight.sol (L610-611)
```text
            uint256 price = IOracle(_collateralParam.oracle).price();
            if (i == collateralIndex) liquidatedCollatPrice = price;
```

**File:** src/Midnight.sol (L612-613)
```text
            uint256 _collateral = _position.collateral[i];
            maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(_collateralParam.lltv, WAD);
```

**File:** src/Midnight.sol (L614-616)
```text
            badDebt = badDebt.zeroFloorSub(
                _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
            );
```

**File:** src/Midnight.sol (L626-641)
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
            _marketState.continuousFeeCredit = _lossFactor < type(uint128).max
                ? UtilsLib.toUint128(
                    _marketState.continuousFeeCredit
                        .mulDivDown(type(uint128).max - _marketState.lossFactor, type(uint128).max - _lossFactor)
                )
                : 0;
        }
```

**File:** src/Midnight.sol (L649-650)
```text
            if (seizedAssets > 0) {
                repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
```

**File:** src/Midnight.sol (L670-676)
```text
            uint128 newCollateral = _position.collateral[collateralIndex] - UtilsLib.toUint128(seizedAssets);
            _position.collateral[collateralIndex] = newCollateral;
            if (newCollateral == 0 && seizedAssets > 0) {
                _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
            }
            _marketState.withdrawable += UtilsLib.toUint128(repaidUnits);
            _position.debt -= UtilsLib.toUint128(repaidUnits);
```

**File:** src/libraries/UtilsLib.sol (L34-36)
```text
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
```

**File:** certora/specs/NoDivisionByZero.spec (L123-125)
```text
    // Assume that the collateral price is non-zero and the collateral is active. Otherwise, liquidate may revert with div by zero.
    require ghostPrice(market.collateralParams[collateralIndex].oracle) > 0, "Assumption: the collateral price is not zero";
    require summaryGetBit(currentContract.position[globalId][borrower].collateralBitmap, collateralIndex), "Assumption: liquidated collateral was activated";
```

**File:** src/interfaces/IOracle.sol (L6-6)
```text
    function price() external view returns (uint256);
```
