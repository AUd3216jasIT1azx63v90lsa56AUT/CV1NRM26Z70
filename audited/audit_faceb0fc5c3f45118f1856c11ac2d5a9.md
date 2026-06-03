Audit Report

## Title
Zero-price oracle enables free collateral seizure with full bad-debt socialization to lenders - (`src/Midnight.sol`)

## Summary
When an oracle returns `0` for a collateral's price, calling `liquidate` with `seizedAssets > 0` computes `repaidUnits = 0` via integer rounding in `mulDivUp`, simultaneously writing off the borrower's entire debt as bad debt (socializing the loss to lenders via `lossFactor`) and transferring the borrower's collateral to the caller while pulling zero loan tokens. Every existing guard is bypassed by the zero-price condition, and the protocol's own token safety requirements guarantee the zero-amount `safeTransferFrom` succeeds.

## Finding Description

**Root cause — `mulDivUp` with zero price (`src/Midnight.sol` line 650):**

`mulDivUp` is defined as `(x * y + (d - 1)) / d` (`src/libraries/UtilsLib.sol` lines 34–35).

With `liquidatedCollatPrice = 0`:
- `seizedAssets.mulDivUp(0, ORACLE_PRICE_SCALE)` = `(0 + ORACLE_PRICE_SCALE − 1) / ORACLE_PRICE_SCALE` = `0`
- `0.mulDivUp(WAD, lif)` = `(0 + lif − 1) / lif` = `0`

So `repaidUnits = 0`.

**Liquidatability check passes (lines 607–624):** With price = 0 for all oracles, `maxDebt` accumulates `mulDivDown(_collateral, 0, ORACLE_PRICE_SCALE) * lltv / WAD = 0`. Thus `originalDebt > maxDebt` reduces to `originalDebt > 0`, which is already guaranteed by the `NotBorrower` check at line 596.

**Full bad debt is realized (lines 614–641):** `badDebt` starts at `originalDebt` and is reduced by `zeroFloorSub(_collateral.mulDivUp(0, ORACLE_PRICE_SCALE)...) = zeroFloorSub(0) = 0` for every collateral. So `badDebt = originalDebt`. The entire debt is written off: `_position.debt -= badDebt`, `lossFactor` is updated to socialize the full loss to lenders, and `totalUnits` is decremented.

**Collateral seizure path executes (lines 643–677):** The branch `seizedAssets > 0` is entered. With `repaidUnits = 0`:
- `_position.collateral[collateralIndex] -= seizedAssets` — collateral removed from borrower
- `_marketState.withdrawable += 0` — pool receives no compensation
- `_position.debt -= 0` — already zeroed by bad debt write-off
- `SafeTransferLib.safeTransfer(collateralToken, receiver, seizedAssets)` (line 696) — collateral sent to attacker
- `SafeTransferLib.safeTransferFrom(loanToken, payer, address(this), 0)` (line 717) — zero loan tokens pulled

**All existing guards are bypassed:**
- `atMostOneNonZero(0, seizedAssets)` at line 595 → passes (input `repaidUnits` param = 0, `seizedAssets` > 0)
- `_position.debt > 0` at line 596 → passes
- `NotLiquidatable` at lines 620–624 → passes (price = 0 makes `maxDebt = 0`)
- RCF check at lines 659–667: after bad debt write-off `_position.debt = 0`, `maxDebt = 0`, so `maxRepaid = 0` and `repaidUnits = 0 ≤ 0` passes
- No `require(liquidatedCollatPrice > 0)` guard exists anywhere in `liquidate`

**Formal proof gap confirmed:** `certora/specs/NoDivisionByZero.spec` line 124 explicitly requires `ghostPrice(...) > 0` as a precondition, meaning the formal proofs do not cover the price = 0 scenario. `certora/specs/Reverts.spec` lines 246–253 only proves that `liquidate` with `repaidUnits > 0` reverts when oracle returns 0 (because line 652 divides by `liquidatedCollatPrice`); it does not prove that `seizedAssets > 0` reverts.

The intended behavior when oracle returns 0 is demonstrated in `test/LiquidationTest.sol` lines 875–876: call `liquidate(market, 0, 0, 0, ...)` (both inputs zero) to realize bad debt only, after which the borrower withdraws their own collateral. The `seizedAssets > 0` path with price = 0 is unguarded.

## Impact Explanation
Lenders suffer a complete, irreversible loss on the borrower's full debt socialized via `lossFactor` write-down. The attacker receives the borrower's real ERC-20 collateral tokens for free. The protocol's `withdrawable` pool receives zero compensation. This constitutes direct theft of collateral value from the lending pool and renders the affected market insolvent. The protocol's own token safety requirements (`src/Midnight.sol` lines 135–136) guarantee the zero-amount `safeTransferFrom` succeeds unconditionally.

## Likelihood Explanation
Preconditions: (1) an oracle transiently returns `0` — explicitly modeled as reachable in the Certora specs (`forceOracleReturnZero`, `singleZeroOracle`); (2) the borrower has `debt > 0` and non-zero collateral at the targeted index. No special privilege is required unless `liquidatorGate` is configured. Oracles can return `0` due to uninitialized price feeds, stale/gap conditions, sequencer downtime, or error-handling paths that return 0 instead of reverting. The exploit is repeatable for any such market and requires no victim cooperation. The protocol's own formal verification explicitly acknowledges this scenario as reachable and leaves the `seizedAssets > 0` path unproven.

## Recommendation
Add a guard at the start of the `seizedAssets > 0` branch (or before entering the `repaidUnits > 0 || seizedAssets > 0` block) requiring `liquidatedCollatPrice > 0`:

```solidity
require(liquidatedCollatPrice > 0, ZeroCollateralPrice());
```

Alternatively, add this check immediately after `liquidatedCollatPrice` is assigned at line 611. This mirrors the existing protection in the `repaidUnits > 0` path (which naturally reverts on division by zero at line 652) and closes the gap acknowledged in `certora/specs/NoDivisionByZero.spec` line 124.

## Proof of Concept

1. Deploy a market with a collateral oracle that can be set to return `0`.
2. Have a borrower deposit collateral and take on debt (`_position.debt > 0`).
3. Set the oracle to return `0`.
4. Call `liquidate(market, collateralIndex, seizedAssets=<borrower's full collateral>, repaidUnits=0, borrower, false, attacker, address(0), "")`.
5. Observe: `repaidUnits` computed as `0`; borrower's full debt written off as bad debt; `lossFactor` updated to socialize loss to lenders; collateral transferred to attacker; zero loan tokens pulled from attacker.

This is directly reproducible by modifying `test/LiquidationTest.sol`'s `testFullBadDebtWithdrawCollateral` (lines 870–890): instead of calling `liquidate` with both inputs zero, call it with `seizedAssets = collateral` and `repaidUnits = 0`, and assert the attacker receives the collateral while paying nothing. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** src/Midnight.sol (L133-136)
```text
/// TOKEN SAFETY REQUIREMENTS
/// @dev List of assumptions on tokens that guarantee that Midnight behaves as expected:
/// - It should be ERC-20 compliant, except that it can omit return values on transfer and transferFrom. In particular,
/// it should not revert because a transfer is no-op.
```

**File:** src/Midnight.sol (L607-624)
```text
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

        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
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

**File:** src/Midnight.sol (L696-717)
```text
        SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);

        if (callback != address(0)) {
            require(
                ILiquidateCallback(callback)
                    .onLiquidate(
                        msg.sender,
                        id,
                        market,
                        collateralIndex,
                        seizedAssets,
                        repaidUnits,
                        borrower,
                        receiver,
                        data,
                        badDebt
                    ) == CALLBACK_SUCCESS,
                WrongLiquidateCallbackReturnValue()
            );
        }

        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
```

**File:** src/libraries/UtilsLib.sol (L33-36)
```text
    /// @dev Returns (x * y) / d rounded up.
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

**File:** test/LiquidationTest.sol (L875-876)
```text
        Oracle(market.collateralParams[0].oracle).setPrice(0);
        midnight.liquidate(market, 0, 0, 0, borrower, false, address(this), address(0), "");
```
