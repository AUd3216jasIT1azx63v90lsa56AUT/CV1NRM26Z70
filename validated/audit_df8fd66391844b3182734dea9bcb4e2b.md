### Title
`liquidate()` Computes `repaidUnits = 0` When Oracle Returns Zero for `seizedAssets` Input Path — (File: src/Midnight.sol)

---

### Summary

In `Midnight.liquidate()`, when the caller supplies `seizedAssets > 0` (instead of `repaidUnits > 0`), the computed `repaidUnits` is derived from `liquidatedCollatPrice`. If the oracle for the liquidated collateral returns `0`, the formula silently produces `repaidUnits = 0`. The liquidator then seizes real collateral while repaying zero debt units, draining the borrower's collateral and leaving lenders with no corresponding loan-token recovery.

---

### Finding Description

**Root cause — asymmetric handling of a zero oracle price**

`liquidatedCollatPrice` is declared as `uint256` and defaults to `0`:

```solidity
uint256 liquidatedCollatPrice;   // line 603
```

It is only assigned inside the collateral loop when `i == collateralIndex`:

```solidity
uint256 price = IOracle(_collateralParam.oracle).price();
if (i == collateralIndex) liquidatedCollatPrice = price;   // line 610-611
```

If the oracle returns `0`, `liquidatedCollatPrice` stays `0`.

The two input branches then behave asymmetrically:

```solidity
if (seizedAssets > 0) {
    // line 650 — multiplies by 0, result is 0; no revert
    repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE)
                              .mulDivUp(WAD, lif);
} else {
    // line 652 — divides by 0; reverts (documented in LIVENESS)
    seizedAssets = repaidUnits.mulDivDown(lif, WAD)
                              .mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
}
```

`mulDivUp(x, 0, d)` in `UtilsLib` evaluates to `(x*0 + d-1)/d = (d-1)/d = 0` for any `d ≥ 2`. So with `liquidatedCollatPrice = 0` and `seizedAssets > 0`, `repaidUnits` is computed as `0`.

The protocol's own LIVENESS comment acknowledges only the `repaidUnits`-input case:

> *"If the liquidated collateral oracle returns 0 on price, liquidate with repaid input reverts."*

The `seizedAssets`-input case is not mentioned and is not protected.

**Downstream state after the call with `repaidUnits = 0`:**

```solidity
_marketState.withdrawable += 0;   // line 675 — no loan tokens added for lenders
_position.debt           -= 0;   // line 676 — borrower debt unchanged
_position.collateral[collateralIndex] -= seizedAssets;  // line 670 — collateral IS seized
```

The liquidator receives `seizedAssets` collateral tokens via `safeTransfer` (line 696) and pays `0` loan tokens (line 717: `safeTransferFrom(..., 0)`).

---

### Impact Explanation

- **Direct theft of collateral**: An attacker calls `liquidate()` with `seizedAssets = borrower.collateral[collateralIndex]` and `repaidUnits = 0`. They receive the full collateral balance for free.
- **Lender loss**: `withdrawable` is not increased, so lenders receive no loan tokens in exchange for the seized collateral. The market's accounting is permanently corrupted.
- **Borrower harm**: The borrower's debt is not reduced despite losing all collateral at that index.
- **Bad-debt overstatement**: With `price = 0`, the `badDebt` loop also fails to subtract the collateral's value (`zeroFloorSub(0)` is a no-op), so `badDebt` is overstated, triggering unnecessary lender slashing on top of the free seizure.

Severity: **High** — direct, irreversible loss of collateral assets with no loan-token compensation.

---

### Likelihood Explanation

The trigger is `IOracle(...).price()` returning `0`. This can occur via:

1. **Oracle manipulation** (explicitly in scope per SECURITY.md): A flash-loan attack on a low-liquidity spot-price oracle can push the reported price to 0 or near-0 in a single block.
2. **Oracle failure / deprecation**: Chainlink aggregators return `0` for deprecated or circuit-broken feeds; custom oracles may return `0` on error instead of reverting.

The attacker needs only: (a) a market whose collateral oracle can be driven to `0`, and (b) a borrower with non-zero collateral at that index who is liquidatable (unhealthy or post-maturity). No privileged access is required. The call is permissionless.

---

### Recommendation

Add an explicit guard in the `seizedAssets > 0` branch, mirroring the implicit revert that already protects the `repaidUnits > 0` branch:

```solidity
if (seizedAssets > 0) {
    require(liquidatedCollatPrice > 0, ZeroOraclePrice());
    repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE)
                              .mulDivUp(WAD, lif);
}
```

This makes both branches consistent: a zero oracle price always reverts, preventing free collateral seizure.

---

### Proof of Concept

**Setup:**
- Market with one collateral (index 0), oracle `MockOracle` (controllable).
- Borrower supplies 1000 collateral tokens, takes 500 debt units.
- Oracle initially returns a normal price; position is healthy.

**Attack steps:**

1. Attacker (or oracle manipulation) causes `MockOracle.price()` to return `0`.
2. With `price = 0`, `maxDebt = 0`, so `originalDebt (500) > maxDebt (0)` → position is liquidatable.
3. Attacker calls:
   ```solidity
   midnight.liquidate(
       market,
       0,              // collateralIndex
       1000,           // seizedAssets = full collateral
       0,              // repaidUnits = 0 (input)
       borrower,
       false,          // normal mode
       attacker,
       address(0),
       ""
   );
   ```
4. Inside `liquidate()`:
   - `liquidatedCollatPrice = 0`
   - `repaidUnits = mulDivUp(1000, 0, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif) = 0`
   - `_position.collateral[0] -= 1000` → borrower loses all collateral
   - `_marketState.withdrawable += 0` → lenders receive nothing
   - `safeTransfer(collateralToken, attacker, 1000)` → attacker receives 1000 tokens
   - `safeTransferFrom(loanToken, attacker, address(this), 0)` → attacker pays nothing

**Expected outcome:** Attacker holds 1000 collateral tokens at zero cost. Borrower retains 500 debt units with no collateral. Lenders' `withdrawable` is unchanged. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/Midnight.sol (L143-146)
```text
/// @dev If an activated collateral oracle reverts on price, liquidate reverts.
/// @dev If an activated collateral oracle reverts on price, isHealthy, withdrawCollateral and take revert when the user
/// (seller for take) has non-zero debt.
/// @dev If the liquidated collateral oracle returns 0 on price, liquidate with repaid input reverts.
```

**File:** src/Midnight.sol (L602-611)
```text
        uint256 maxDebt;
        uint256 liquidatedCollatPrice;
        uint256 originalDebt = _position.debt;
        uint256 badDebt = originalDebt;
        uint128 _collateralBitmap = _position.collateralBitmap;
        while (_collateralBitmap != 0) {
            uint256 i = UtilsLib.msb(_collateralBitmap);
            CollateralParams memory _collateralParam = market.collateralParams[i];
            uint256 price = IOracle(_collateralParam.oracle).price();
            if (i == collateralIndex) liquidatedCollatPrice = price;
```

**File:** src/Midnight.sol (L649-653)
```text
            if (seizedAssets > 0) {
                repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
            } else {
                seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
            }
```

**File:** src/Midnight.sol (L670-677)
```text
            uint128 newCollateral = _position.collateral[collateralIndex] - UtilsLib.toUint128(seizedAssets);
            _position.collateral[collateralIndex] = newCollateral;
            if (newCollateral == 0 && seizedAssets > 0) {
                _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
            }
            _marketState.withdrawable += UtilsLib.toUint128(repaidUnits);
            _position.debt -= UtilsLib.toUint128(repaidUnits);
        }
```

**File:** src/libraries/UtilsLib.sol (L34-36)
```text
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
```
