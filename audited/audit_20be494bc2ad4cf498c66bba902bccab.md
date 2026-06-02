The code confirms the vulnerability. Let me verify the exact arithmetic before producing the final output.

**Bad-debt loop** (lines 614–616): `badDebt` is reduced by `mulDivUp(collateral * price / ORACLE_PRICE_SCALE, WAD, maxLif)`, so after line 628, `_position.debt ≈ Σ collateral[i] * price_i / ORACLE_PRICE_SCALE * WAD / maxLif_i`.

**lif ramp** (lines 645–647): When `postMaturityMode=true` and `Δt < TIME_TO_MAX_LIF (900 s)`, `lif = WAD + (maxLif - WAD) * Δt / 900 < maxLif`.

**repaidUnits** (line 650): `mulDivUp(seizedAssets * price / ORACLE_PRICE_SCALE, WAD, lif)` — larger per unit than the debt ceiling set by the bad-debt step because `lif < maxLif`.

**Guard** (lines 655–668): The `maxRepaid` cap is unconditionally skipped when `postMaturityMode == true`.

**Underflow** (line 676): `_position.debt -= toUint128(repaidUnits)` reverts.

Concrete check with the submitted values: `_position.debt = 451`, `repaidUnits = mulDivUp(901, 1e18, 1001111111111111111) = 901`. `451 - 901` → arithmetic underflow revert. Confirmed.

---

Audit Report

## Title
Post-maturity `seizedAssets` liquidation reverts on bad-debt positions when `lif < maxLif` - (File: `src/Midnight.sol`)

## Summary
When a position carries bad debt and a liquidator calls `liquidate` with `seizedAssets > 0` and `postMaturityMode = true` before `TIME_TO_MAX_LIF` (900 s) has elapsed, the bad-debt reduction at line 628 uses `maxLif` to shrink `_position.debt`, while `repaidUnits` at line 650 is computed with the smaller ramping `lif`. Because `lif < maxLif`, `repaidUnits` exceeds the reduced `_position.debt`, and the unchecked subtraction at line 676 reverts with an arithmetic underflow. The `maxRepaid` guard that would cap `repaidUnits` is unconditionally skipped in `postMaturityMode`.

## Finding Description

**Root cause:** The bad-debt loop (lines 607–618) computes each collateral's maximum supportable debt using `maxLif` (line 615):

```solidity
badDebt = badDebt.zeroFloorSub(
    _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
);
```

After subtracting `badDebt` at line 628, `_position.debt` is bounded by `Σ collateral[i] * price_i / ORACLE_PRICE_SCALE * WAD / maxLif_i`.

However, when `block.timestamp < market.maturity + TIME_TO_MAX_LIF`, the `lif` computed at lines 645–647 is strictly less than `maxLif`:

```solidity
uint256 lif = postMaturityMode
    ? UtilsLib.min(_maxLif, WAD + (_maxLif - WAD) * (block.timestamp - market.maturity) / TIME_TO_MAX_LIF)
    : _maxLif;
```

The `seizedAssets` branch at line 650 then computes:

```solidity
repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
```

Since `lif < maxLif`, `repaidUnits` is larger per unit of collateral than the debt ceiling established by the bad-debt step. The `maxRepaid` / RCF guard at lines 655–668 is entirely skipped because it is gated on `!postMaturityMode`. The subtraction `_position.debt -= UtilsLib.toUint128(repaidUnits)` at line 676 therefore underflows and reverts.

**Concrete verification** (`ORACLE_PRICE_SCALE = 1e36`, `WAD = 1e18`, `TIME_TO_MAX_LIF = 900 s`):

| Parameter | Value |
|---|---|
| `collateral[0]` | 901 |
| `price_0` | `ORACLE_PRICE_SCALE` |
| `maxLif_0` | `2 * WAD` |
| `originalDebt` | 452 |
| `Δt` | 1 s |

- `mulDivUp(901, WAD, 2*WAD) = ceil(901/2) = 451`; `badDebt = 452 − 451 = 1`; `_position.debt → 451`
- `lif = WAD + WAD *

The code confirms the vulnerability. Let me verify the key lines:

- Line 615: bad-debt uses `maxLif` (mulDivUp) to compute max supportable debt
- Line 628: `_position.debt -= uint128(badDebt)` — debt reduced to `≈ collateral * price / maxLif`
- Lines 645–647: `lif` ramps from `WAD` to `maxLif` over `TIME_TO_MAX_LIF = 15 minutes`; when `Δt < 900s`, `lif < maxLif`
- Line 650: `repaidUnits = seizedAssets.mulDivUp(...).mulDivUp(WAD, lif)` — larger per unit than the `maxLif`-bounded debt ceiling
- Lines 655–668: `maxRepaid` guard is entirely skipped when `postMaturityMode == true`
- Line 676: `_position.debt -= UtilsLib.toUint128(repaidUnits)` — underflows

The concrete example is arithmetically correct: `repaidUnits = 901 > 451 = _position.debt` → revert.

---

Audit Report

## Title
Post-maturity `seizedAssets` liquidation reverts on bad-debt positions when `lif < maxLif` - (File: `src/Midnight.sol`)

## Summary
When a position carries bad debt and a liquidator calls `liquidate` with `seizedAssets > 0` and `postMaturityMode = true` before `TIME_TO_MAX_LIF` (900 s) has elapsed, the bad-debt reduction uses `maxLif` to shrink `_position.debt`, while `repaidUnits` is computed with the smaller ramping `lif`. Because `lif < maxLif`, `repaidUnits` exceeds the reduced `_position.debt`, and the unchecked subtraction at line 676 reverts with an arithmetic underflow. The only guard that would cap `repaidUnits` (`maxRepaid`, lines 655–668) is unconditionally skipped in `postMaturityMode`.

## Finding Description
**Root cause:** The bad-debt loop (lines 607–618) computes each collateral's maximum supportable debt using `maxLif` via `mulDivUp` (line 615):

```solidity
badDebt = badDebt.zeroFloorSub(
    _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
);
```

After subtracting `badDebt` at line 628, `_position.debt` is bounded by `Σ collateral[i] * price_i / ORACLE_PRICE_SCALE * WAD / maxLif_i`.

When `postMaturityMode = true` and `block.timestamp - market.maturity < TIME_TO_MAX_LIF`, the `lif` computed at lines 645–647 is strictly less than `maxLif`:

```solidity
uint256 lif = postMaturityMode
    ? UtilsLib.min(_maxLif, WAD + (_maxLif - WAD) * (block.timestamp - market.maturity) / TIME_TO_MAX_LIF)
    : _maxLif;
```

The `seizedAssets` branch at line 650 then computes:

```solidity
repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
```

Because `lif < maxLif`, `repaidUnits` per unit of collateral exceeds the debt ceiling established by the bad-debt step. The `maxRepaid`/RCF guard (lines 655–668) is entirely skipped because it is gated on `!postMaturityMode`. The subtraction at line 676:

```solidity
_position.debt -= UtilsLib.toUint128(repaidUnits);
```

underflows and reverts. `UtilsLib.toUint128` only checks for uint128 overflow, not for the subtraction underflow.

**Concrete verification** (`ORACLE_PRICE_SCALE = 1e36`, `WAD = 1e18`, `TIME_TO_MAX_LIF = 900 s`):

| Parameter | Value |
|---|---|
| `collateral[0]` | 901 |
| `price_0` | `ORACLE_PRICE_SCALE` |
| `maxLif_0` | `2 * WAD` |
| `originalDebt` | 452 |
| `Δt` | 1 s |

- `badDebt = 452 − ceil(901/2) = 452 − 451 = 1`; `_position.debt → 451`
- `lif = WAD + WAD/900 = 1_001_111_111_111_111_111`
- `repaidUnits = ceil(901 * 1e18 / 1_001_111_111_111_111_111) = 901`
- `901 > 451` → **arithmetic underflow revert at line 676**

## Impact Explanation
The `seizedAssets`-input liquidation path is completely broken for any position with bad debt during the 15-minute post-maturity ramp window. Any liquidator using this path receives an unconditional revert, delaying timely liquidation of bad-debt positions during the most critical post-maturity window. This constitutes severe degradation of protocol liveness and liquidation availability — a core safety mechanism of the protocol.

## Likelihood Explanation
All three preconditions are reachable by any unprivileged user without special access:
1. Bad debt is a normal protocol outcome when collateral value falls below `debt * maxLif / price`.
2. The 15-minute ramp window is a fixed protocol constant (`TIME_TO_MAX_LIF = 15 minutes` in `src/libraries/ConstantsLib.sol`).
3. Any liquidator using the collateral-input form (`seizedAssets > 0`) hits this deterministically within the window.

The revert is deterministic and requires no coordination or privileged action.

## Recommendation
In the `postMaturityMode` branch, cap `repaidUnits` at `_position.debt` before the subtraction at line 676. A minimal fix after line 650:

```solidity
if (postMaturityMode) {
    repaidUnits = UtilsLib.min(repaidUnits, _position.debt);
    // Recompute seizedAssets if it was derived from repaidUnits, or accept the rounding.
}
```

Alternatively, apply a symmetric cap analogous to the `maxRepaid` guard: after computing `repaidUnits` in the `postMaturityMode && seizedAssets > 0` branch, add `repaidUnits = UtilsLib.min(repaidUnits, _position.debt)` and adjust `seizedAssets` accordingly to maintain consistency.

## Proof of Concept
Deploy a market with one collateral (`maxLif = 2e18`, `lltv` any allowed value). Supply 901 collateral units and borrow 452 debt units. Advance time past `market.maturity` by exactly 1 second. Call `liquidate(market, 0, 901, 0, borrower, true, receiver, address(0), "")`. The call reverts with an arithmetic underflow at line 676 of `src/Midnight.sol`. Advancing time by `TIME_TO_MAX_LIF` (900 s) or more causes `lif = maxLif` and the underflow disappears, confirming the ramp window as the trigger.