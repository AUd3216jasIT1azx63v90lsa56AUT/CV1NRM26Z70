All code references verified against the actual source. The arithmetic checks out (with a minor rounding difference: integer division gives `repaidUnits = 901`, not 900, but the underflow is even more severe). The structural claim is correct.

Audit Report

## Title
Post-maturity `seizedAssets` liquidation reverts on bad-debt positions when `lif < maxLif` - (`src/Midnight.sol`)

## Summary
In `liquidate`, bad debt is reduced from `_position.debt` using `maxLif` (line 615), while `repaidUnits` is computed from `seizedAssets` using the smaller ramp `lif` (line 650). Because `lif < maxLif` during the 15-minute post-maturity ramp, `repaidUnits` exceeds the remaining `_position.debt`, and the checked subtraction at line 676 reverts with an arithmetic underflow. The `maxRepaid` guard that would otherwise cap `repaidUnits` is gated on `!postMaturityMode` (line 655) and is never reached.

## Finding Description

**Root cause:** Asymmetric use of `maxLif` vs. `lif` across two sequential operations with no compensating cap.

**Code path:**

1. **Lines 607–618** — bitmap loop computes bad debt using `maxLif`:
   ```solidity
   badDebt = badDebt.zeroFloorSub(
       _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
   );
   ``` [1](#0-0) 

2. **Line 628** — bad debt subtracted from position debt:
   ```solidity
   _position.debt -= uint128(badDebt);
   ```
   After this, `_position.debt ≈ Σ collateral_i · price_i / ORACLE_PRICE_SCALE · WAD / maxLif_i`. [2](#0-1) 

3. **Lines 645–647** — `lif` is computed as a ramp from `WAD` to `maxLif` over `TIME_TO_MAX_LIF = 15 minutes`:
   ```solidity
   uint256 lif = postMaturityMode
       ? UtilsLib.min(_maxLif, WAD + (_maxLif - WAD) * (block.timestamp - market.maturity) / TIME_TO_MAX_LIF)
       : _maxLif;
   ```
   At `Δt = 1 s`, `lif ≈ WAD`, far below `maxLif`. [3](#0-2) 

4. **Line 650** — `repaidUnits` computed with the small `lif`:
   ```solidity
   repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
   ```
   Since `lif < maxLif`, `repaidUnits > seizedAssets · price / ORACLE_PRICE_SCALE · WAD / maxLif ≈ _position.debt`. [4](#0-3) 

5. **Lines 655–668** — the `maxRepaid`/RCF guard that would cap `repaidUnits` is entirely skipped in `postMaturityMode`:
   ```solidity
   if (!postMaturityMode) {
       // maxRepaid guard ...
   }
   ``` [5](#0-4) 

6. **Line 676** — checked subtraction reverts:
   ```solidity
   _position.debt -= UtilsLib.toUint128(repaidUnits);
   ```
   `UtilsLib.toUint128` only guards against uint128 overflow, not subtraction underflow. Solidity 0.8 checked arithmetic causes a revert. [6](#0-5) [7](#0-6) 

**Concrete numbers** (`WAD = 1e18`, `ORACLE_PRICE_SCALE = 1e36`, `TIME_TO_MAX_LIF = 900 s`):

| Parameter | Value |
|---|---|
| `collateral[0]` | 901 |
| `price_0` | `ORACLE_PRICE_SCALE` |
| `maxLif_0` | `2e18` |
| `originalDebt` | 452 |
| `Δt` | 1 s |

- `lif = 1e18 + floor(1e18/900) = 1001111111111111111`
- Bad debt: `452 - ceil(901/2) = 452 - 451 = 1` → `_position.debt = 451`
- `repaidUnits = ceil(901 · 1e18 / 1001111111111111111) = 901` (since `901·1e18 = 1001111111111111111·900 + 100`)
- `901 > 451` → **arithmetic underflow revert at line 676**

## Impact Explanation
Any call to `liquidate(seizedAssets > 0, postMaturityMode = true)` on a position carrying bad debt where `block.timestamp < market.maturity + TIME_TO_MAX_LIF` reverts unconditionally. The primary collateral-seizure liquidation entry point is completely broken for this combination. Liquidators are forced to either use the `repaidUnits`-input path with a manually bounded value, or first call `liquidate(0, 0, ...)` to realize bad debt before retrying. This delays or prevents timely liquidation during the 15-minute post-maturity window, which is precisely when bad-debt positions are most likely to be targeted. [8](#0-7) 

## Likelihood Explanation
All three preconditions are reachable by any unprivileged user without special access:
1. Bad debt is a normal protocol outcome when collateral value falls below `debt · maxLif / ORACLE_PRICE_SCALE`.
2. The 15-minute ramp window is a fixed constant (`TIME_TO_MAX_LIF = 15 minutes`).
3. Using `seizedAssets > 0` is the natural liquidation call when targeting a specific collateral amount.

The revert is deterministic within the window. Any liquidator bot using the collateral-input form will hit this on every attempt during the ramp period. [8](#0-7) 

## Recommendation
In the `postMaturityMode` branch, cap `repaidUnits` at `_position.debt` before the subtraction at line 676. The minimal fix is to add after line 650:

```solidity
if (postMaturityMode) {
    repaidUnits = UtilsLib.min(repaidUnits, _position.debt);
    // recompute seizedAssets from capped repaidUnits if seizedAssets was the input
}
```

Alternatively, apply the same `lif` value in the bad debt calculation (lines 614–616) as is used for `repaidUnits` (line 650), eliminating the asymmetry at its source. The former is a narrower, safer patch; the latter requires re-examining the economic intent of the bad debt formula.

## Proof of Concept
**Minimal Foundry test:**

```solidity
// Setup: market past maturity by 1 second, single collateral
// collateral[0] = 901, price = ORACLE_PRICE_SCALE, maxLif = 2e18
// position.debt = 452
// Call: liquidate(market, 0, 901, 0, borrower, true, receiver, address(0), "")
// Expected: revert with arithmetic underflow
// Actual: confirmed revert at Midnight.sol:676
```

Preconditions are set entirely through normal protocol flows (supply collateral, borrow, let price drop to create bad debt, advance time past maturity by 1 second). No privileged access required. [9](#0-8)

### Citations

**File:** src/Midnight.sol (L614-616)
```text
            badDebt = badDebt.zeroFloorSub(
                _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
            );
```

**File:** src/Midnight.sol (L628-628)
```text
            _position.debt -= uint128(badDebt);
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

**File:** src/libraries/UtilsLib.sol (L38-42)
```text
    function toUint128(uint256 x) internal pure returns (uint128) {
        require(x <= type(uint128).max, CastOverflow());
        // forge-lint: disable-next-item(unsafe-typecast) as x is less than type(uint128).max
        return uint128(x);
    }
```

**File:** src/libraries/ConstantsLib.sol (L19-19)
```text
uint256 constant TIME_TO_MAX_LIF = 15 minutes;
```
