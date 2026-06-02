Audit Report

## Title
Double `mulDivUp` in `seizedAssets` path produces `repaidUnits > _position.debt`, causing arithmetic underflow revert in `liquidate` — (`src/Midnight.sol`)

## Summary
When `liquidate` is called with `seizedAssets > 0` and `postMaturityMode = true`, `repaidUnits` is computed via two consecutive ceiling-rounding operations at line 650. Because the RCF check (lines 655–668) is entirely skipped in `postMaturityMode`, there is no guard preventing `repaidUnits` from exceeding `_position.debt`. The unchecked subtraction at line 676 then reverts with an arithmetic underflow, blocking legitimate liquidations of post-maturity positions.

## Finding Description

**Exact code path:**

Line 650 computes `repaidUnits` with double ceiling rounding: [1](#0-0) 

The RCF block — which contains the only `repaidUnits <= maxRepaid` guard — is gated behind `!postMaturityMode` and is entirely skipped for post-maturity liquidations: [2](#0-1) 

The collateral underflow check at line 670 only validates `seizedAssets` against collateral, not `repaidUnits` against debt: [3](#0-2) 

The unchecked (in the Solidity 0.8 sense: no explicit `unchecked {}` block, so it reverts) subtraction at line 676 has no prior cap: [4](#0-3) 

**Root cause:** `mulDivUp(seizedAssets * price / ORACLE_PRICE_SCALE, WAD, lif)` rounds up whenever `seizedAssets * price * WAD` is not divisible by `lif`. For any `lif > WAD` (which is always true just after maturity, since `lif = WAD + (_maxLif - WAD) * Δt / TIME_TO_MAX_LIF`), the result can exceed the remaining debt by 1 unit. No guard `repaidUnits <= _position.debt` exists anywhere in the function.

**Concrete exploit flow:**

1. Market: `lltv = 0.77e18`, `price = ORACLE_PRICE_SCALE`, `maxLif ≈ 1.061e18`.
2. Borrower: `collateral = 2`, `debt = 1`. Position is healthy pre-maturity.
3. Time advances past `market.maturity` → post-maturity liquidatable.
4. Bad-debt loop (lines 614–616): `badDebt = 1.zeroFloorSub(ceil(2 * WAD / maxLif)) = 1.zeroFloorSub(2) = 0`. Debt remains 1. [5](#0-4) 
5. Liquidator calls `liquidate(seizedAssets=2, repaidUnits=0, postMaturityMode=true)`.
6. `lif = WAD + (maxLif - WAD) * 1 / TIME_TO_MAX_LIF` (one second after maturity) → `lif = WAD + ε`.
7. `repaidUnits = mulDivUp(2, ORACLE_PRICE_SCALE, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif) = mulDivUp(2, WAD, WAD+ε) = ceil(2·WAD / (WAD+ε)) = 2` (rounds up since not divisible).
8. `_position.debt -= 2` → `1 - 2` → arithmetic underflow → **revert**. [6](#0-5) 

**Why existing checks fail:**
- The only `repaidUnits`-bounding check (RCF, lines 662–667) is inside `if (!postMaturityMode)` and is never reached.
- The collateral check at line 670 passes because `seizedAssets = 2 = collateral`.
- There is no `require(repaidUnits <= _position.debt)` anywhere in the function (confirmed by code inspection).

## Impact Explanation
Any post-maturity position where `ceil(collateral * price / ORACLE_PRICE_SCALE * WAD / lif) > _position.debt` (after bad-debt realization) becomes permanently un-liquidatable via the `seizedAssets` input path. Liquidators cannot seize collateral or clear debt, violating the core protocol invariant that post-maturity positions must remain liquidatable. This is a denial-of-service on the liquidation mechanism for an entire class of positions.

## Likelihood Explanation
The condition is reachable by any unprivileged liquidator on any post-maturity market. It requires no special oracle value (`price = ORACLE_PRICE_SCALE` is a normal value), no privileged access, and no victim mistake. It is most likely with small integer collateral/debt amounts where a 1-unit rounding error is significant relative to remaining debt, but the rounding condition (`seizedAssets * WAD` not divisible by `lif`) holds for the vast majority of `(seizedAssets, lif)` pairs since `lif` is a time-dependent value unlikely to divide evenly.

## Recommendation
Before line 676, cap `repaidUnits` to the remaining debt:

```solidity
if (repaidUnits > _position.debt) repaidUnits = _position.debt;
```

Alternatively, use `zeroFloorSub` for the debt update:
```solidity
_position.debt = UtilsLib.toUint128(_position.debt.zeroFloorSub(repaidUnits));
```

This mirrors the conservative rounding already applied in the bad-debt computation and ensures the subtraction never underflows regardless of rounding in the `seizedAssets` path.

## Proof of Concept
Minimal Foundry test:
```solidity
// Setup: market past maturity, borrower with collateral=2, debt=1, price=ORACLE_PRICE_SCALE
// lif = WAD + 1 (one time unit after maturity)
// Call: liquidate(market, collatIdx, 2, 0, borrower, true, receiver, address(0), "")
// Expected: revert with arithmetic underflow at Midnight.sol:676
// Actual: revert confirmed — repaidUnits=2 > _position.debt=1
```

The existing `testLiquidateCollateralInput` fuzz test in `test/LiquidationTest.sol` avoids this by bounding `seized` to prevent `repaidUnits > debt`, but the contract itself enforces no such bound. [7](#0-6)

### Citations

**File:** src/Midnight.sol (L614-616)
```text
            badDebt = badDebt.zeroFloorSub(
                _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
            );
```

**File:** src/Midnight.sol (L643-647)
```text
        if (repaidUnits > 0 || seizedAssets > 0) {
            uint256 _maxLif = market.collateralParams[collateralIndex].maxLif;
            uint256 lif = postMaturityMode
                ? UtilsLib.min(_maxLif, WAD + (_maxLif - WAD) * (block.timestamp - market.maturity) / TIME_TO_MAX_LIF)
                : _maxLif;
```

**File:** src/Midnight.sol (L649-650)
```text
            if (seizedAssets > 0) {
                repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
```

**File:** src/Midnight.sol (L655-668)
```text
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
