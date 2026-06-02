All critical code paths have been verified. Here is the validation result:

---

Audit Report

## Title
Zero-price oracle allows collateral seizure with zero debt repayment via `seizedAssets` input path - (`src/Midnight.sol`)

## Summary

When the oracle for the liquidated collateral returns exactly 0, calling `liquidate` with `seizedAssets > 0` and `repaidUnits = 0` causes `repaidUnits` to be silently computed as 0 at line 650 without reverting. The function then transfers `seizedAssets` collateral tokens to the receiver while paying zero loan tokens, violating the invariant that collateral seizure must correspond to proportional debt repayment. The symmetric `repaidUnits > 0` input path correctly reverts via division-by-zero at line 652, but the `seizedAssets > 0` path has no equivalent guard.

## Finding Description

**Root cause — missing post-computation zero check at line 650:**

`mulDivUp` is defined as `(x * y + (d - 1)) / d`. [1](#0-0) 

When `liquidatedCollatPrice = 0` and `seizedAssets > 0`:

```
Step 1: seizedAssets.mulDivUp(0, ORACLE_PRICE_SCALE)
      = (seizedAssets * 0 + (1e36 - 1)) / 1e36
      = (1e36 - 1) / 1e36
      = 0  (integer division, no revert)

Step 2: 0.mulDivUp(WAD, lif)
      = (0 * WAD + (lif - 1)) / lif
      = (lif - 1) / lif
      = 0  (since lif >= WAD = 1e18 >> 1, no revert)
```

So `repaidUnits = 0` silently at line 650. [2](#0-1) 

**Contrast with the `repaidUnits > 0` path:**

`mulDivDown(ORACLE_PRICE_SCALE, 0)` = `(x * ORACLE_PRICE_SCALE) / 0` → division-by-zero → reverts. This path is correctly blocked and formally proven in `Reverts.spec` lines 246–253. [3](#0-2) 

No analogous rule exists for the `seizedAssets > 0` input path.

**Exploit flow — single-collateral position:**

1. Borrower has `_position.debt > 0` and `_position.collateral[idx] > 0`; oracle at `idx` returns 0.
2. `maxDebt = 0` → `originalDebt > maxDebt` → `NotLiquidatable` check passes (line 620–624).
3. `badDebt = originalDebt` → `_position.debt` zeroed at line 628; `lossFactor` updated (lenders absorb the loss).
4. Attacker calls `liquidate(market, idx, seizedAssets>0, 0, borrower, false, receiver, address(0), "")`.
5. `atMostOneNonZero(0, seizedAssets>0)` → passes (line 595).
6. `repaidUnits` computed as 0 (arithmetic above).
7. RCF check (line 662–667): `0 <= maxRepaid` → trivially passes.
8. Line 670–676: `_position.collateral[idx] -= seizedAssets`, `_marketState.withdrawable += 0`, `_position.debt -= 0`.
9. Line 696: `safeTransfer(collateralToken, receiver, seizedAssets)` — collateral transferred to attacker.
10. Line 717: `safeTransferFrom(loanToken, payer, address(this), 0)` — zero loan tokens paid.

The borrower's residual collateral — which they are entitled to withdraw after bad debt realization (as confirmed by `testFullBadDebtWithdrawCollateral` line 890) — is stolen by the attacker in the same transaction. [4](#0-3) 

**Exploit flow — multi-collateral position (more impactful):**

If the position has collateral A (zero-priced oracle) and collateral B (non-zero price, but position is still unhealthy: `originalDebt > maxDebt`), then `badDebt` may be 0 (B covers all debt coverage), so `_position.debt` is NOT zeroed before the seizure block. The attacker seizes collateral A for free while `_position.debt` remains completely unchanged — a direct debt-free collateral grab with no bad-debt accounting at all. [5](#0-4) 

**All existing checks are insufficient:**

- `atMostOneNonZero` (line 595): checks input values only, not computed values.
- `NotLiquidatable` (line 620–624): passes because price=0 makes `maxDebt` contribution from this collateral = 0.
- RCF check (line 662–667): `repaidUnits=0 <= maxRepaid` always passes.
- No check anywhere that `repaidUnits > 0` after computation when `seizedAssets > 0`.

**Formal verification gap confirmed:**

`NoDivisionByZero.spec` line 124 explicitly requires `ghostPrice > 0` as a precondition, acknowledging the zero-price case is unverified. [6](#0-5) 

`Reverts.spec` lines 246–253 only proves revert for the `repaidUnits > 0` input; no analogous rule exists for `seizedAssets > 0`. [7](#0-6) 

## Impact Explanation

An unprivileged liquidator seizes an arbitrary amount of collateral tokens (up to `_position.collateral[collateralIndex]`) while transferring zero loan tokens. In the single-collateral case, the borrower's residual collateral (which they are entitled to withdraw post-bad-debt-realization) is stolen with no compensation. In the multi-collateral case, `_position.debt` is completely unchanged after the call — the borrower loses real collateral with no debt reduction and no protocol accounting correction. The protocol's core invariant — collateral seizure corresponds to proportional debt repayment — is broken. Collateral tokens may have real value even when the oracle temporarily returns 0 (e.g., depegged or stale oracle).

## Likelihood Explanation

**Required preconditions:**

1. A market collateral's oracle returns 0. The protocol explicitly handles this as a valid scenario: `testFullBadDebtWithdrawCollateral` sets price to 0 and calls `liquidate`; `oracleZeroCausesIsHealthyReturnFalse` formally proves `isHealthy` returns false at price=0. This is not oracle failure — it is a valid price for a worthless or depegged asset.
2. The borrower must have `_position.debt > 0` (trivially satisfied for any active borrower).
3. The position must be liquidatable (`originalDebt > maxDebt` or post-maturity). Price=0 guarantees this for any single-collateral position.

Any unprivileged liquidator can execute this in a single external transaction. No special role, signature, or governance action is required. Repeatable as long as the oracle returns 0 and collateral remains.

## Recommendation

Add a post-computation check after line 650 to revert when `seizedAssets > 0` but `repaidUnits` computes to 0:

```solidity
if (seizedAssets > 0) {
    repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
    require(repaidUnits > 0, ZeroRepaidUnits()); // guard against zero-price oracle
}
```

This mirrors the protection already provided by the division-by-zero revert on the `repaidUnits > 0` path and closes the asymmetry. Additionally, update `NoDivisionByZero.spec` and `Reverts.spec` to add a formal rule proving that `liquidate` with `seizedAssets > 0` and a zero-price oracle reverts.

## Proof of Concept

Minimal Foundry test (extend `LiquidationTest.sol`):

```solidity
function testSeizeCollateralZeroPriceZeroRepay() public {
    uint256 units = 1000e18;
    collateralize(market, borrower, units);
    setupMarket(market, units);

    // Set oracle to 0 — valid scenario per testFullBadDebtWithdrawCollateral
    Oracle(market.collateralParams[0].oracle).setPrice(0);

    uint256 collateralBefore = midnight.collateral(id, borrower, 0);
    uint256 attackerLoanBefore = loanToken.balanceOf(address(this));

    // Attacker calls liquidate with seizedAssets > 0, repaidUnits = 0
    midnight.liquidate(market, 0, collateralBefore, 0, borrower, false, address(this), address(0), "");

    // Attacker received all collateral
    assertEq(collateralToken.balanceOf(address(this)), collateralBefore);
    // Attacker paid zero loan tokens
    assertEq(loanToken.balanceOf(address(this)), attackerLoanBefore);
    // Borrower's collateral is gone
    assertEq(midnight.collateral(id, borrower, 0), 0);
}
```

### Citations

**File:** src/libraries/UtilsLib.sol (L34-36)
```text
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
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

**File:** src/Midnight.sol (L649-653)
```text
            if (seizedAssets > 0) {
                repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
            } else {
                seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
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

**File:** certora/specs/NoDivisionByZero.spec (L123-125)
```text
    // Assume that the collateral price is non-zero and the collateral is active. Otherwise, liquidate may revert with div by zero.
    require ghostPrice(market.collateralParams[collateralIndex].oracle) > 0, "Assumption: the collateral price is not zero";
    require summaryGetBit(currentContract.position[globalId][borrower].collateralBitmap, collateralIndex), "Assumption: liquidated collateral was activated";
```
