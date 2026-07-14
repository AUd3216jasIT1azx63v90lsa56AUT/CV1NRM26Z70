### Title
Arithmetic Underflow in `liquidate()` Bad Debt Realization Blocks Recovery When `totalUnits < badDebt` — (File: src/Midnight.sol)

### Summary

In `Midnight.liquidate()`, when bad debt is realized, the code computes `_totalUnits - badDebt` without underflow protection. After continuous fees are claimed (which reduce `totalUnits` without reducing borrower debt) or after sequential bad debt realizations, `totalUnits` can fall below a borrower's `badDebt`. This causes `liquidate()` to revert unconditionally, permanently blocking bad debt realization and leaving the market in an unrecoverable accounting state.

### Finding Description

The bad debt realization block in `liquidate()` is:

```solidity
if (badDebt > 0) {
    _position.debt -= uint128(badDebt);
    uint256 _totalUnits = _marketState.totalUnits;
    uint256 _lossFactor = _marketState.lossFactor;
    _marketState.lossFactor = UtilsLib.toUint128(
        type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
    );
    _marketState.totalUnits -= UtilsLib.toUint128(badDebt);
``` [1](#0-0) 

Both `_totalUnits - badDebt` (inside `mulDivDown`) and `_marketState.totalUnits -= UtilsLib.toUint128(badDebt)` are plain subtractions. Solidity 0.8 reverts on underflow. There is no `zeroFloorSub` or guard.

**How `totalUnits` falls below `badDebt`:**

`totalUnits` is decremented in two places independent of borrower debt:

1. **Continuous fee claims** — `claimContinuousFee` decrements `totalUnits` without touching any borrower's `debt`:

```solidity
_marketState.continuousFeeCredit -= UtilsLib.toUint128(amount);
_marketState.totalUnits -= UtilsLib.toUint128(amount);
_marketState.withdrawable -= UtilsLib.toUint128(amount);
``` [2](#0-1) 

2. **Sequential bad debt realizations** — each realization decrements `totalUnits` by `badDebt`, but the next borrower's debt is unchanged.

**Concrete trigger path (two-borrower scenario):**

| Step | `totalUnits` | B1.debt | B2.debt |
|------|-------------|---------|---------|
| Initial | 100 | 50 | 50 |
| Fee claimer claims 1 unit | **99** | 50 | 50 |
| Collateral crashes to 0 for both | 99 | 50 | 50 |
| Liquidate B1 (`badDebt=50`) | **49** | 0 | 50 |
| Liquidate B2 (`badDebt=50`) | **49 − 50 → REVERT** | — | — |

The second `liquidate` call reverts at `_totalUnits - badDebt = 49 - 50`. B2's bad debt can never be realized.

### Impact Explanation

- `liquidate()` reverts permanently for any borrower whose `badDebt > totalUnits`.
- Bad debt cannot be socialized via the `lossFactor` mechanism, leaving lender credits overstated relative to actual recoverable value.
- The market enters an unrecoverable state: lenders cannot get accurate credit accounting, and the bad debt remains on the books indefinitely.
- This is a **permanent fund freeze / accounting corruption** for the affected market.

**Impact: High**

### Likelihood Explanation

The trigger requires two ordinary, non-privileged protocol events to coincide:

1. The `feeClaimer` claims accrued continuous fees (a routine, expected operation — not an attack).
2. Multiple borrowers become severely undercollateralized (a realistic market risk, especially in volatile collateral markets).

The continuous fee cap is 1% annualized, so even a small fee claim (e.g., 1 unit on a 100-unit market) is sufficient to create the gap. The second liquidation then reverts if the second borrower's `badDebt` equals their full debt (collateral near zero).

**Likelihood: Medium**

### Recommendation

Replace the plain subtraction with a saturating/floor subtraction, mirroring the pattern already used elsewhere in the codebase (`zeroFloorSub`). When `badDebt >= totalUnits`, the loss factor should be set to `type(uint128).max` (total loss) and `totalUnits` should be set to 0:

```diff
if (badDebt > 0) {
    _position.debt -= uint128(badDebt);
    uint256 _totalUnits = _marketState.totalUnits;
    uint256 _lossFactor = _marketState.lossFactor;
-   _marketState.lossFactor = UtilsLib.toUint128(
-       type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
-   );
-   _marketState.totalUnits -= UtilsLib.toUint128(badDebt);
+   if (badDebt >= _totalUnits) {
+       _marketState.lossFactor = type(uint128).max;
+       _marketState.totalUnits = 0;
+   } else {
+       _marketState.lossFactor = UtilsLib.toUint128(
+           type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
+       );
+       _marketState.totalUnits -= UtilsLib.toUint128(badDebt);
+   }
```

This is consistent with the existing guard `require(_marketState.lossFactor < type(uint128).max, MarketLossFactorMaxedOut())` in `take()`, which already handles the total-loss terminal state. [3](#0-2) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

// Setup: single-collateral market, two borrowers, continuous fee enabled.
// 1. Lender L1 buys 50 units from Borrower B1  → totalUnits = 50, B1.debt = 50
// 2. Lender L2 buys 50 units from Borrower B2  → totalUnits = 100, B2.debt = 50
// 3. feeClaimer calls claimContinuousFee(market, 1, receiver)
//    → totalUnits = 99
// 4. Oracle price crashes to 0 for both borrowers' collateral.
// 5. liquidate(market, 0, 0, 0, B1, false, ...) succeeds:
//    badDebt = 50, totalUnits = 99 - 50 = 49  ✓
// 6. liquidate(market, 0, 0, 0, B2, false, ...) REVERTS:
//    badDebt = 50, totalUnits = 49 - 50 → arithmetic underflow
//    → B2's bad debt is permanently unrealizable.
//    → lossFactor is never updated for B2's loss.
//    → Market accounting is permanently corrupted.
``` [4](#0-3)

### Citations

**File:** src/Midnight.sol (L318-320)
```text
        _marketState.continuousFeeCredit -= UtilsLib.toUint128(amount);
        _marketState.totalUnits -= UtilsLib.toUint128(amount);
        _marketState.withdrawable -= UtilsLib.toUint128(amount);
```

**File:** src/Midnight.sol (L349-349)
```text
        require(_marketState.lossFactor < type(uint128).max, MarketLossFactorMaxedOut());
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
