The exploit path is fully traceable. Let me confirm the exact arithmetic and the gap in formal verification coverage before writing the final output.

### Title
Zero-price oracle allows collateral seizure with zero debt repayment via `seizedAssets` input path - (`src/Midnight.sol`)

### Summary

When the oracle for the liquidated collateral returns exactly 0, calling `liquidate` with `seizedAssets > 0` and `repaidUnits = 0` causes `repaidUnits` to be computed as 0 at line 650 without reverting. The function then transfers `seizedAssets` collateral tokens to the receiver while leaving `_position.debt` completely unchanged, violating the invariant that collateral seizure must correspond to proportional debt repayment. The symmetric `repaidUnits > 0` input path correctly reverts via division-by-zero at line 652, but the `seizedAssets > 0` path has no equivalent guard.

### Finding Description

**Root cause — missing post-computation zero check at line 650:**

```solidity
// src/Midnight.sol:649-650
if (seizedAssets > 0) {
    repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
}
```

When `liquidatedCollatPrice = 0`:

- Step 1: `seizedAssets.mulDivUp(0, ORACLE_PRICE_SCALE)` = `(seizedAssets * 0 + (ORACLE_PRICE_SCALE - 1)) / ORACLE_PRICE_SCALE` = `(1e36 - 1) / 1e36` = **0** (integer division, no revert)
- Step 2: `0.mulDivUp(WAD, lif)` = `(0 * WAD + (lif - 1)) / lif` = `(lif - 1) / lif` = **0** (since `lif >= WAD = 1e18 >> 1`, no revert)

So `repaidUnits = 0` silently.

**Contrast with the `repaidUnits > 0` path (line 652):**

```solidity
seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
```

`mulDivDown(ORACLE_PRICE_SCALE, 0)` → division by zero → reverts. This path is correctly blocked, and `Reverts.spec` line 246–253 formally proves it. But the `seizedAssets > 0` path has no analogous protection and is explicitly excluded from the formal proof.

**Full exploit flow (pre-maturity, single-collateral position):**

1. Borrower has `_position.debt > 0` and `_position.collateral[idx] > 0` with oracle at `idx` returning 0.
2. Oracle returning 0 → `maxDebt = 0` → `originalDebt > maxDebt` → `NotLiquidatable` check passes.
3. `badDebt = originalDebt` (all debt is bad debt) → bad debt is realized, `_position.debt` reduced to 0 at line 628.
4. Attacker calls `liquidate(market, idx, seizedAssets>0, 0, borrower, false, receiver, address(0), "")`.
5. `atMostOneNonZero(0, seizedAssets>0)` → passes.
6. `repaidUnits` computed as 0 (shown above).
7. RCF check line 662–667: `repaidUnits (0) <= maxRepaid` → passes trivially.
8. Line 670–676: `_position.collateral[idx] -= seizedAssets`, `_marketState.withdrawable += 0`, `_position.debt -= 0`.
9. Line 696: `safeTransfer(collateralToken, receiver, seizedAssets)` — collateral transferred.
10. Line 717: `safeTransferFrom(loanToken, payer, address(this), 0)` — zero loan tokens paid.

**Wait — step 3 interaction:** When `badDebt = originalDebt` (price = 0, single collateral), `_position.debt` is already zeroed at line 628 before the `seizedAssets` block. The `seizedAssets > 0` block still executes (line 643: `repaidUnits > 0 || seizedAssets > 0`), seizes collateral, and subtracts 0 from an already-zero debt. The collateral is seized for free after bad debt realization.

**Multi-collateral variant (more impactful):** If the position has a second collateral with a non-zero oracle price that keeps `originalDebt > maxDebt` (position unhealthy but not fully bad debt), `_position.debt` is NOT zeroed before the seizure block. The attacker seizes the zero-priced collateral while `_position.debt` remains unchanged — a direct debt-free collateral grab.

**Existing checks reviewed and insufficient:**
- `atMostOneNonZero` (line 595): checks input values only, not computed values.
- `NotLiquidatable` (line 620–624): passes because price=0 makes `maxDebt` contribution from this collateral = 0.
- RCF check (line 662–667): `repaidUnits=0 <= maxRepaid` always passes.
- No check anywhere that `repaidUnits > 0` after computation when `seizedAssets > 0`.

**Formal verification gap confirmed:** `NoDivisionByZero.spec` line 124 explicitly requires `ghostPrice > 0` as a precondition, acknowledging the zero-price case is unverified. `Reverts.spec` lines 246–253 only proves revert for the `repaidUnits > 0` input; no analogous rule exists for `seizedAssets > 0`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

### Impact Explanation

An unprivileged liquidator seizes an arbitrary amount of collateral tokens (up to `_position.collateral[collateralIndex]`) while transferring zero loan tokens. In the multi-collateral case where the zero-priced collateral is not the sole collateral, `_position.debt` is completely unchanged after the call — the borrower loses real collateral with no debt reduction. In the single-collateral case, bad debt is first realized (debt zeroed), then the collateral is seized for free. Either way, the protocol's core invariant — that collateral seizure corresponds to proportional debt repayment — is broken. [5](#0-4) [6](#0-5) 

### Likelihood Explanation

**Preconditions:**
1. A market collateral's oracle returns 0. The protocol explicitly handles this as a valid scenario: `testFullBadDebtWithdrawCollateral` (line 875) sets price to 0 and calls `liquidate`; `oracleZeroCausesIsHealthyReturnFalse` formally proves `isHealthy` returns false at price=0. This is not oracle failure — it is a valid price for a worthless or depegged asset.
2. The borrower must have `_position.debt > 0` (trivially satisfied for any active borrower).
3. The position must be liquidatable (`originalDebt > maxDebt` or post-maturity). Price=0 guarantees this for any single-collateral position; for multi-collateral, the position must already be unhealthy on other collaterals.

**Feasibility:** Any unprivileged liquidator can execute this. No special role, signature, or governance action is required. The call is a single external transaction. Repeatable as long as the oracle returns 0 and collateral remains. [7](#0-6) [8](#0-7) 

### Recommendation

After computing `repaidUnits` from `seizedAssets` at line 650, add a guard that reverts when the oracle price is zero (i.e., when `repaidUnits` computes to 0 despite `seizedAssets > 0`):

```solidity
if (seizedAssets > 0) {
    repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
    require(repaidUnits > 0, ZeroRepaidUnits()); // add this
}
```

This mirrors the natural revert that already protects the `repaidUnits > 0` path (division by zero at line 652) and closes the asymmetry. Alternatively, require `liquidatedCollatPrice > 0` before entering the `seizedAssets > 0` branch. The corresponding Certora rule `oracleZeroCausesLiquidateWithRepaidRevert` should be extended to cover the `seizedAssets > 0` input path. [9](#0-8) [3](#0-2) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
// import Midnight, Market, Oracle, helpers as in LiquidationTest.sol

contract ZeroPriceSeizureTest is Test {
    // Setup: single-collateral market, borrower has debt, oracle set to 0.
    function testFuzz_ZeroPriceSeizure(uint128 seizedAssets) public {
        seizedAssets = uint128(bound(seizedAssets, 1, type(uint128).max));

        // 1. Deploy market with collateral oracle returning 0.
        // 2. Borrower supplies collateral and borrows (debt > 0).
        // 3. Set oracle price to 0.
        Oracle(market.collateralParams[0].oracle).setPrice(0);

        uint256 debtBefore = midnight.debtOf(id, borrower);
        uint256 collateralBefore = midnight.collateral(id, borrower, 0);
        uint256 receiverBalanceBefore = collateralToken.balanceOf(receiver);

        // 4. Attacker calls liquidate with seizedAssets > 0, repaidUnits = 0.
        (uint256 seized, uint256 repaid) =
            midnight.liquidate(market, 0, seizedAssets, 0, borrower, false, receiver, address(0), "");

        // 5. Assert: collateral transferred, debt unchanged, zero loan tokens paid.
        assertEq(repaid, 0, "repaid must be 0");
        assertGt(seized, 0, "seized must be > 0");
        assertEq(midnight.debtOf(id, borrower), debtBefore, "debt must be unchanged");
        assertEq(
            collateralToken.balanceOf(receiver),
            receiverBalanceBefore + seized,
            "receiver got collateral for free"
        );
    }
}
```

**Expected assertions:** `repaid == 0`, `seized > 0`, `debtOf(borrower)` unchanged (or reduced only by bad-debt realization, not by proportional repayment), receiver balance increased by `seized` with zero loan tokens paid. The test should pass — demonstrating the invariant violation. [10](#0-9) [11](#0-10)

### Citations

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/Midnight.sol (L643-677)
```text
        if (repaidUnits > 0 || seizedAssets > 0) {
            uint256 _maxLif = market.collateralParams[collateralIndex].maxLif;
            uint256 lif = postMaturityMode
                ? UtilsLib.min(_maxLif, WAD + (_maxLif - WAD) * (block.timestamp - market.maturity) / TIME_TO_MAX_LIF)
                : _maxLif;

            if (seizedAssets > 0) {
                repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
            } else {
                seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
            }

            if (!postMaturityMode) {
                uint256 lltv = market.collateralParams[collateralIndex].lltv;
                // Note that debt >= maxDebt in this branch.
                // The imprecision in this computation is at most a few hundreds collateral or loan token assets.
                uint256 maxRepaid = lltv < WAD
                    ? (_position.debt - maxDebt).mulDivUp(WAD * WAD, WAD * WAD - lif * lltv)
                    : type(uint256).max;
                require(
                    repaidUnits <= maxRepaid
                        || _position.collateral[collateralIndex].mulDivDown(liquidatedCollatPrice, ORACLE_PRICE_SCALE)
                            .mulDivDown(WAD, lif).zeroFloorSub(maxRepaid) < market.rcfThreshold,
                    RecoveryCloseFactorConditionsViolated()
                );
            }

            uint128 newCollateral = _position.collateral[collateralIndex] - UtilsLib.toUint128(seizedAssets);
            _position.collateral[collateralIndex] = newCollateral;
            if (newCollateral == 0 && seizedAssets > 0) {
                _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
            }
            _marketState.withdrawable += UtilsLib.toUint128(repaidUnits);
            _position.debt -= UtilsLib.toUint128(repaidUnits);
        }
```

**File:** src/Midnight.sol (L696-696)
```text
        SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
```

**File:** src/Midnight.sol (L717-717)
```text
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
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

**File:** certora/specs/NoDivisionByZero.spec (L123-125)
```text
    // Assume that the collateral price is non-zero and the collateral is active. Otherwise, liquidate may revert with div by zero.
    require ghostPrice(market.collateralParams[collateralIndex].oracle) > 0, "Assumption: the collateral price is not zero";
    require summaryGetBit(currentContract.position[globalId][borrower].collateralBitmap, collateralIndex), "Assumption: liquidated collateral was activated";
```

**File:** test/LiquidationTest.sol (L870-892)
```text
    function testFullBadDebtWithdrawCollateral(uint256 units) public {
        units = bound(units, 10, MAX_UNITS);
        collateralize(market, borrower, units);
        setupMarket(market, units);

        Oracle(market.collateralParams[0].oracle).setPrice(0);
        midnight.liquidate(market, 0, 0, 0, borrower, false, address(this), address(0), "");

        assertEq(midnight.debtOf(id, borrower), 0, "debt");
        assertEq(midnight.totalUnits(id), 0, "total units");
        uint128 _lossFactor = midnight.lossFactor(id);
        assertEq(_lossFactor, type(uint128).max, "loss factor");
        midnight.updatePosition(market, lender);
        assertEq(midnight.creditOf(id, lender), 0, "credit after slashing");

        // withdrawCollateral still works
        uint256 collateral = midnight.collateral(id, borrower, 0);
        assertGt(collateral, 0, "has collateral");
        vm.prank(borrower);
        midnight.setIsAuthorized(address(this), true, borrower);
        midnight.withdrawCollateral(market, 0, collateral, borrower, borrower);
        assertEq(midnight.collateral(id, borrower, 0), 0, "collateral withdrawn");
    }
```
