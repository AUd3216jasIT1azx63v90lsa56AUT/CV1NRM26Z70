### Title
Liquidation Permanently Blocked by Arithmetic Underflow When `badDebt > totalUnits` After Continuous Fee Claims — (File: `src/Midnight.sol`)

---

### Summary

In `Midnight.sol`, the `liquidate` function computes `_totalUnits - badDebt` in checked Solidity 0.8 arithmetic at line 632. The `claimContinuousFee` function legitimately reduces `marketState[id].totalUnits` below a borrower's outstanding debt. When a borrower's collateral subsequently crashes, `badDebt` (bounded by `_position.debt`) exceeds `_totalUnits`, causing an arithmetic underflow revert that permanently blocks liquidation and locks remaining collateral in the contract.

---

### Finding Description

**Vulnerability type:** DoS / Permanent lock of liquidation path (accounting/state-transition)

**Root cause — `_totalUnits - badDebt` underflow:**

Inside `liquidate`, when `badDebt > 0`, the protocol updates the market's loss factor:

```solidity
// src/Midnight.sol lines 629–634
uint256 _totalUnits = _marketState.totalUnits;
uint256 _lossFactor  = _marketState.lossFactor;
_marketState.lossFactor = UtilsLib.toUint128(
    type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
);
_marketState.totalUnits -= UtilsLib.toUint128(badDebt);
``` [1](#0-0) 

`_totalUnits - badDebt` is evaluated in **checked arithmetic** (Solidity 0.8 default). `mulDivDown` contains no `unchecked` block:

```solidity
// src/libraries/UtilsLib.sol line 29-31
function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y) / d;
}
``` [2](#0-1) 

**How `badDebt > _totalUnits` is reached:**

`badDebt` is initialised to `_position.debt` and reduced by the collateral value at `maxLif`:

```solidity
// src/Midnight.sol lines 604–617
uint256 badDebt = originalDebt;
// ...
badDebt = badDebt.zeroFloorSub(
    _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
);
``` [3](#0-2) 

`_totalUnits` is reduced by `claimContinuousFee`, a normal protocol operation performed by the `feeClaimer`:

```solidity
// src/Midnight.sol lines 318–320
_marketState.continuousFeeCredit -= UtilsLib.toUint128(amount);
_marketState.totalUnits          -= UtilsLib.toUint128(amount);
_marketState.withdrawable        -= UtilsLib.toUint128(amount);
``` [4](#0-3) 

`_updatePosition` accrues fees into `continuousFeeCredit` **without** touching `totalUnits`:

```solidity
// src/Midnight.sol line 846
marketState[id].continuousFeeCredit += UtilsLib.toUint128(accruedFee);
``` [5](#0-4) 

So the accounting identity is:

```
totalUnits = Σ(lenders' credit) + continuousFeeCredit + withdrawable
```

After `claimContinuousFee(amount)`, `totalUnits` shrinks by `amount` while `_position.debt` of any borrower is **unchanged**. Once claimed fees exceed the borrower's collateral value at `maxLif`, the condition `badDebt > _totalUnits` holds and every call to `liquidate` reverts.

**End-to-end exploit path (no privileged attacker required):**

1. Lender takes a sell offer: `totalUnits = 100`, borrower's `debt = 100`.
2. Continuous fees accrue over time: `continuousFeeCredit = 10`, lender's credit drops to 90, `totalUnits` stays at 100.
3. `feeClaimer` calls `claimContinuousFee(10)` (routine protocol operation): `totalUnits = 90`.
4. Borrower's collateral price crashes to 0 (or near 0): `badDebt = 100`.
5. Any liquidator calls `liquidate(...)`: execution reaches `_totalUnits - badDebt = 90 - 100` → **underflow revert**.
6. Liquidation is permanently blocked; remaining collateral is locked.

---

### Impact Explanation

- **Severity: High** — Liquidation of any position where `badDebt > totalUnits` is permanently impossible. The borrower's remaining collateral (however small) is locked in the contract and irrecoverable. Bad debt cannot be realised and socialised among lenders, corrupting the market's accounting indefinitely.
- The `NotLiquidatable()` guard at line 620–624 is passed successfully (the position is genuinely unhealthy), but the revert occurs deeper in the bad-debt accounting block, so no workaround exists within the current code. [6](#0-5) 

---

### Likelihood Explanation

- **Likelihood: Low-Medium.** The trigger requires two independent events: (a) the `feeClaimer` claiming accumulated continuous fees (a routine, expected operation), and (b) a borrower's collateral crashing severely. Neither event is attacker-controlled; both are normal market conditions. In markets with high continuous fees and volatile collateral, this combination is realistic over the market's lifetime.
- `MAX_CONTINUOUS_FEE = 0.01e18 / 365 days` (~1% APR), so over a 100-day market, up to ~0.27% of credit can be claimed as fees — enough to trigger the underflow if collateral drops to near zero. [7](#0-6) 

---

### Recommendation

Cap `badDebt` to `_totalUnits` before the subtraction, or use `zeroFloorSub` to prevent the underflow:

```diff
// src/Midnight.sol ~line 632
+ uint256 effectiveBadDebt = UtilsLib.min(badDebt, _totalUnits);
  _marketState.lossFactor = UtilsLib.toUint128(
-     type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
+     type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - effectiveBadDebt, _totalUnits)
  );
- _marketState.totalUnits -= UtilsLib.toUint128(badDebt);
+ _marketState.totalUnits -= UtilsLib.toUint128(effectiveBadDebt);
```

If `badDebt > _totalUnits`, the loss factor should be set to `type(uint128).max` (total loss), consistent with the existing `MarketLossFactorMaxedOut` handling.

---

### Proof of Concept

**Setup:**
- Single-collateral market, `lltv = 0.98e18`, `maxLif ≈ 1.01e18`, `continuousFee = MAX_CONTINUOUS_FEE`.
- Lender takes a sell offer for 1000 units; borrower's `debt = 1000`, `totalUnits = 1000`.

**Steps:**
1. Advance time by 50 days. `continuousFeeCredit` accrues ≈ 1.37 units. Call `_updatePosition` for lender; lender's credit drops to ≈ 998.63.
2. `feeClaimer` calls `claimContinuousFee(market, 1, receiver)`: `totalUnits = 999`.
3. Oracle for collateral returns `price = 0` (collateral crash).
4. Call `liquidate(market, 0, 0, 0, borrower, false, receiver, address(0), "")`:
   - `originalDebt = 1000`, `maxDebt = 0` → liquidatable.
   - `badDebt = 1000` (collateral value = 0).
   - `_totalUnits = 999`.
   - `_totalUnits - badDebt = 999 - 1000` → **arithmetic underflow, revert**.
5. No liquidator can ever liquidate this position. Collateral (if any) is permanently locked. [8](#0-7)

### Citations

**File:** src/Midnight.sol (L318-320)
```text
        _marketState.continuousFeeCredit -= UtilsLib.toUint128(amount);
        _marketState.totalUnits -= UtilsLib.toUint128(amount);
        _marketState.withdrawable -= UtilsLib.toUint128(amount);
```

**File:** src/Midnight.sol (L604-617)
```text
        uint256 originalDebt = _position.debt;
        uint256 badDebt = originalDebt;
        uint128 _collateralBitmap = _position.collateralBitmap;
        while (_collateralBitmap != 0) {
            uint256 i = UtilsLib.msb(_collateralBitmap);
            CollateralParams memory _collateralParam = market.collateralParams[i];
            uint256 price = IOracle(_collateralParam.oracle).price();
            if (i == collateralIndex) liquidatedCollatPrice = price;
            uint256 _collateral = _position.collateral[i];
            maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(_collateralParam.lltv, WAD);
            badDebt = badDebt.zeroFloorSub(
                _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
            );
            _collateralBitmap = _collateralBitmap.clearBit(i);
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
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

**File:** src/Midnight.sol (L846-846)
```text
        marketState[id].continuousFeeCredit += UtilsLib.toUint128(accruedFee);
```

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** src/libraries/ConstantsLib.sol (L18-18)
```text
uint32 constant MAX_CONTINUOUS_FEE = uint32(uint256(0.01e18) / uint256(365 days));
```
