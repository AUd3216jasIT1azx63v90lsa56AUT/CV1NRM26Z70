The code matches all claims in the report. Let me verify the concrete arithmetic:

**Bad-debt step** (line 615): `ceil(901 × 1e36 / 1e36 × 1e18 / 2e18) = ceil(901/2) = 451` → `badDebt = 452 − 451 = 1` → `_position.debt = 451` [1](#0-0) 

**`lif` ramp** (lines 645–647) at `Δt = 1 s`: `1e18 + 1e18 × 1 / 900 = 1_001_111_111_111_111_111` (strictly < `maxLif = 2e18`) [2](#0-1) 

**`repaidUnits`** (line 650): `ceil(901 × 1e18 / 1_001_111_111_111_111_111)`. Since `1_001_111_111_111_111_111 × 900 + 100 = 901e18`, `repaidUnits = 901`. [3](#0-2) 

**Guard skipped** (line 655): `if (!postMaturityMode)` unconditionally bypasses the `maxRepaid`/RCF cap in `postMaturityMode`. [4](#0-3) 

**Underflow** (line 676): `451 − 901` reverts under Solidity 0.8+ checked arithmetic. [5](#0-4) 

`TIME_TO_MAX_LIF = 15 minutes` confirmed at line 19. [6](#0-5) 

All six code references are exact, the arithmetic is reproducible, the exploit path requires no privileges, and no existing guard prevents it. The finding is valid.

---

Audit Report

## Title
Post-maturity `seizedAssets` liquidation reverts on bad-debt positions when `lif < maxLif` - (File: `src/Midnight.sol`)

## Summary
When a position carries bad debt and a liquidator calls `liquidate` with `seizedAssets > 0` and `postMaturityMode = true` within the 15-minute `TIME_TO_MAX_LIF` ramp window, the bad-debt reduction at line 628 uses `maxLif` to shrink `_position.debt`, while `repaidUnits` at line 650 is computed with the smaller ramping `lif`. Because `lif < maxLif`, `repaidUnits` can exceed the reduced `_position.debt`, and the unchecked subtraction at line 676 reverts with an arithmetic underflow. The `maxRepaid`/RCF guard that would otherwise cap `repaidUnits` is unconditionally skipped in `postMaturityMode`.

## Finding Description
**Root cause:** The bad-debt loop (lines 607–618) computes each collateral's maximum supportable debt using `maxLif`:

```solidity
badDebt = badDebt.zeroFloorSub(
    _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
);
```

After subtracting `badDebt` at line 628, `_position.debt` is bounded by `Σ collateral[i] × price_i / ORACLE_PRICE_SCALE × WAD / maxLif_i`.

However, when `block.timestamp < market.maturity + TIME_TO_MAX_LIF`, the `lif` computed at lines 645–647 is strictly less than `maxLif`:

```solidity
uint256 lif = postMaturityMode
    ? UtilsLib.min(_maxLif, WAD + (_maxLif - WAD) * (block.timestamp - market.maturity) / TIME_TO_MAX_LIF)
    : _maxLif;
```

The `seizedAssets` branch at line 650 then computes `repaidUnits = seizedAssets.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif)`. Because `lif < maxLif`, each unit of collateral yields more `repaidUnits` than the debt ceiling established by the bad-debt step.

The `maxRepaid`/RCF guard at lines 655–668 is entirely skipped because it is gated on `!postMaturityMode`. No other guard caps `repaidUnits` relative to `_position.debt`.

The subtraction at line 676 therefore underflows and reverts:

```solidity
_position.debt -= UtilsLib.toUint128(repaidUnits);
```

## Impact Explanation
Any liquidator calling `liquidate(seizedAssets > 0, postMaturityMode = true)` on a bad-debt position during the 15-minute post-maturity ramp window receives an unconditional revert. The collateral-seizure entry point is completely broken for this combination of conditions, delaying timely liquidation during a critical window and leaving bad-debt positions unresolvable via the primary liquidation path.

## Likelihood Explanation
All three preconditions are reachable by any unprivileged user: bad debt is a normal protocol outcome when collateral value falls below `debt × maxLif / ORACLE_PRICE_SCALE`; the 15-minute ramp window is a fixed protocol constant (`TIME_TO_MAX_LIF = 15 minutes`, `ConstantsLib.sol` line 19); and any liquidator using the `seizedAssets`-input form hits this deterministically within the window. The revert is deterministic and requires no coordination or privileged action.

## Recommendation
In the `seizedAssets` branch, cap `repaidUnits` to `_position.debt` before the subtraction at line 676, or compute `repaidUnits` using `maxLif` (consistent with the bad-debt step) rather than the ramping `lif`. Alternatively, extend the `maxRepaid`/RCF guard to also apply in `postMaturityMode` when bad debt has been written down, ensuring `repaidUnits` cannot exceed the post-write-down `_position.debt`.

## Proof of Concept
Deploy a market with one collateral (`maxLif = 2e18`, `lltv` any allowed value). Create a borrower position with `collateral[0] = 901`, `debt = 452`. Advance time to `market.maturity + 1` (1 second past maturity, within the 15-minute ramp). Call `liquidate(seizedAssets = 901, postMaturityMode = true)`. The transaction reverts with an arithmetic underflow at line 676. Computed values: `badDebt = 1`, `_position.debt → 451`, `lif = 1_001_111_111_111_111_111`, `repaidUnits = ceil(901e18 / lif) = 901`, `451 − 901` underflows.

### Citations

**File:** src/Midnight.sol (L614-616)
```text
            badDebt = badDebt.zeroFloorSub(
                _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
            );
```

**File:** src/Midnight.sol (L645-647)
```text
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

**File:** src/Midnight.sol (L676-676)
```text
            _position.debt -= UtilsLib.toUint128(repaidUnits);
```

**File:** src/libraries/ConstantsLib.sol (L19-19)
```text
uint256 constant TIME_TO_MAX_LIF = 15 minutes;
```
