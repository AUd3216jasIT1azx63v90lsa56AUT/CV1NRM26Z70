Audit Report

## Title
Post-maturity bad-debt liquidation reverts with arithmetic underflow when `lif < maxLif` and `seizedAssets > 0` - (File: src/Midnight.sol)

## Summary
The `liquidate` function uses `maxLif` to write down `_position.debt` via the bad-debt path (line 628), but then computes `repaidUnits` using the strictly smaller ramped `lif` (line 650). Because dividing by a smaller denominator yields a larger quotient, `repaidUnits` exceeds the already-reduced `_position.debt`, and the Solidity 0.8+ checked subtraction at line 676 reverts with an arithmetic underflow. This makes any position carrying bad debt permanently un-liquidatable via the `seizedAssets` path for the entire 15-minute `TIME_TO_MAX_LIF` ramp window.

## Finding Description

**Root cause:** Two inconsistent LIF values are applied to the same collateral value in the same call.

**Step 1 — Bad-debt write-down uses `maxLif` (lines 614–616, 628):**

```solidity
badDebt = badDebt.zeroFloorSub(
    _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
);
// ...
_position.debt -= uint128(badDebt);
```

After line 628, `_position.debt ≈ Σ collateral_i · price_i / ORACLE_PRICE_SCALE · WAD / maxLif_i` (rounded up).

**Step 2 — `lif` is a linear ramp strictly less than `maxLif` during the window (lines 645–647):**

```solidity
uint256 lif = postMaturityMode
    ? UtilsLib.min(_maxLif, WAD + (_maxLif - WAD) * (block.timestamp - market.maturity) / TIME_TO_MAX_LIF)
    : _maxLif;
```

`TIME_TO_MAX_LIF = 15 minutes`. When `block.timestamp - market.maturity < TIME_TO_MAX_LIF`, `lif < maxLif`.

**Step 3 — `repaidUnits` computed with smaller `lif` (line 650):**

```solidity
repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
```

Dividing by `lif < maxLif` yields `repaidUnits > _position.debt`.

**Step 4 — Subtraction reverts (line 676):**

```solidity
_position.debt -= UtilsLib.toUint128(repaidUnits);
```

Solidity 0.8+ checked arithmetic causes a revert on underflow.

**Step 5 — The `maxRepaid`/RCF guard that would cap `repaidUnits` is entirely skipped in post-maturity mode (lines 655–668):**

```solidity
if (!postMaturityMode) {
    // maxRepaid guard — never executed in post-maturity mode
}
```

There is no equivalent upper-bound guard on `repaidUnits` in the post-maturity branch.

**Concrete numeric example** (`ORACLE_PRICE_SCALE = 1e36`, `WAD = 1e18`, single collateral):

| Parameter | Value |
|---|---|
| `collateral` | 1 000 |
| `price` | `ORACLE_PRICE_SCALE` |
| `maxLif` | `1.1e18` |
| `lif` (early ramp) | `1.05e18` |
| `originalDebt` | 1 000 |

- `badDebt = 1000 − ⌈1000/1.1⌉ = 1000 − 910 = 90`
- `_position.debt` after line 628 = **910**
- `repaidUnits = ⌈1000/1.05⌉ = 953`
- Line 676: `910 − 953` → **arithmetic underflow → revert**

## Impact Explanation
Any liquidator calling `liquidate` with `seizedAssets > 0` on a position carrying bad debt during the 15-minute post-maturity ramp window receives a guaranteed revert. The `seizedAssets` path is the natural path for liquidators who want to seize all available collateral. The position cannot be cleared via this path for the entire ramp window, violating the core protocol invariant that unhealthy positions must always remain liquidatable. This constitutes a deterministic, temporary freeze of the liquidation mechanism for affected positions, with direct risk of protocol insolvency if bad-debt positions cannot be cleared promptly.

## Likelihood Explanation
All three preconditions are permissionless and time-based: market maturity is reached naturally, bad debt arises from normal price movements, and the 15-minute window is precisely when liquidation incentives are lowest (lif near WAD) and liquidators are most likely to seize all available collateral via `seizedAssets`. The bug is deterministic and repeatable for any parameter set satisfying `badDebt > 0` and `lif < maxLif`.

## Recommendation
Cap `repaidUnits` at `_position.debt` before the subtraction in the post-maturity branch, or add an equivalent guard analogous to the `maxRepaid` check that already exists for the pre-maturity branch. The simplest fix is:

```solidity
// After computing repaidUnits in the seizedAssets branch:
if (postMaturityMode && repaidUnits > _position.debt) {
    repaidUnits = _position.debt;
}
```

Alternatively, use `maxLif` consistently for both the bad-debt write-down and the `repaidUnits` computation in post-maturity mode, or restructure so that the bad-debt reduction and the `repaidUnits` calculation use the same LIF value.

## Proof of Concept
1. Deploy a market with a single collateral, `maxLif = 1.1e18`, `lltv` any valid tier.
2. Create a borrower position with `collateral = 1000`, `debt = 1000` (at `price = ORACLE_PRICE_SCALE`).
3. Advance `block.timestamp` to `market.maturity + 1` (inside the 15-minute ramp, so `lif ≈ WAD`).
4. Call `liquidate(..., seizedAssets=1000, repaidUnits=0, ..., postMaturityMode=true)`.
5. Observe revert due to arithmetic underflow at line 676.

The revert is deterministic for any `(collateral, price, maxLif, lif)` satisfying `badDebt > 0` and `lif < maxLif`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** src/Midnight.sol (L614-616)
```text
            badDebt = badDebt.zeroFloorSub(
                _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
            );
```

**File:** src/Midnight.sol (L626-628)
```text
        if (badDebt > 0) {
            // forge-lint: disable-next-item(unsafe-typecast) as badDebt <= _position.debt
            _position.debt -= uint128(badDebt);
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

**File:** src/Midnight.sol (L675-676)
```text
            _marketState.withdrawable += UtilsLib.toUint128(repaidUnits);
            _position.debt -= UtilsLib.toUint128(repaidUnits);
```

**File:** src/libraries/ConstantsLib.sol (L19-19)
```text
uint256 constant TIME_TO_MAX_LIF = 15 minutes;
```
