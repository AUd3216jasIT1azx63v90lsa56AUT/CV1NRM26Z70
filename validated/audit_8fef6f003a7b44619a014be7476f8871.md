Audit Report

## Title
Post-maturity `seizedAssets` liquidation reverts on bad-debt positions when `lif < maxLif` - (File: `src/Midnight.sol`)

## Summary
In `liquidate`, bad debt is subtracted from `_position.debt` using `maxLif` (line 615), but `repaidUnits` is then computed from `seizedAssets` using the smaller ramped `lif` (line 650). Because `lif < maxLif` during the 15-minute post-maturity window, `repaidUnits` exceeds the already-reduced `_position.debt`, and the Solidity 0.8 checked subtraction at line 676 reverts with an arithmetic underflow. The `maxRepaid` guard that would otherwise cap `repaidUnits` is gated on `!postMaturityMode` (line 655) and is never reached in this path.

## Finding Description

**Root cause:** Asymmetric use of `maxLif` vs. `lif` across two sequential operations with no compensating cap in `postMaturityMode`.

**Code path:**

1. **Lines 614–616** — bad debt is computed using `maxLif`: [1](#0-0) 

2. **Line 628** — bad debt is subtracted from `_position.debt`, reducing it to approximately `Σ collateral_i · price_i / ORACLE_PRICE_SCALE · WAD / maxLif_i`: [2](#0-1) 

3. **Lines 645–647** — `lif` is computed as a linear ramp from `WAD` to `maxLif` over `TIME_TO_MAX_LIF = 900 s`. At `Δt = 1 s`, `lif ≈ WAD`, far below `maxLif`: [3](#0-2) [4](#0-3) 

4. **Line 650** — `repaidUnits` is computed using the small `lif`. Since `lif < maxLif`, `repaidUnits > seizedAssets · price / ORACLE_PRICE_SCALE · WAD / maxLif ≈ _position.debt`: [5](#0-4) 

5. **Lines 655–668** — the `maxRepaid`/RCF guard that would cap `repaidUnits` is entirely skipped in `postMaturityMode`: [6](#0-5) 

6. **Line 676** — `UtilsLib.toUint128` only guards against uint128 overflow, not subtraction underflow. Solidity 0.8 checked arithmetic causes a revert when `repaidUnits > _position.debt`: [7](#0-6) [8](#0-7) 

**Concrete verification** (`WAD = 1e18`, `ORACLE_PRICE_SCALE = 1e36`, `TIME_TO_MAX_LIF = 900`):

| Parameter | Value |
|---|---|
| `collateral[0]` | 901 |
| `price_0` | `ORACLE_PRICE_SCALE` |
| `maxLif_0` | `2e18` |
| `originalDebt` | 452 |
| `Δt` | 1 s |

- `lif = 1e18 + floor(1e18 / 900) = 1_001_111_111_111_111_111`
- Bad debt: `452 − ⌈901/2⌉ = 452 − 451 = 1` → `_position.debt = 451`
- `repaidUnits = ⌈901 · 1e18 / 1_001_111_111_111_111_111⌉`
  - `1_001_111_111_111_111_111 × 900 = 900_999_999_999_999_999_900`
  - `901 × 1e18 − 900_999_999_999_999_999_900 = 100` → `repaidUnits = 901`
- `901 > 451` → **arithmetic underflow revert at line 676**

## Impact Explanation
Any call to `liquidate(seizedAssets > 0, postMaturityMode = true)` on a position carrying bad debt where `block.timestamp < market.maturity + TIME_TO_MAX_LIF` reverts unconditionally. The collateral-seizure liquidation entry point is completely broken for this combination. Liquidators are forced to either use the `repaidUnits`-input path with a manually bounded value, or first call `liquidate(0, 0, ...)` to realize bad debt before retrying. This delays or prevents timely liquidation during the 15-minute post-maturity window, which is precisely when bad-debt positions are most likely to be targeted.

## Likelihood Explanation
All three preconditions are reachable by any unprivileged user without special access:
1. Bad debt is a normal protocol outcome when collateral value falls below `debt · maxLif / ORACLE_PRICE_SCALE`.
2. The 15-minute ramp window is a fixed constant (`TIME_TO_MAX_LIF = 15 minutes`).
3. Using `seizedAssets > 0` is the natural liquidation call when targeting a specific collateral amount.

The revert is deterministic within the window. Any liquidator bot using the collateral-input form will hit this on every attempt during the ramp period.

## Recommendation
After computing `repaidUnits` in the `seizedAssets > 0` branch (line 650), cap it to `_position.debt` in `postMaturityMode`. For example, add immediately after line 650:

```solidity
if (postMaturityMode) {
    repaidUnits = UtilsLib.min(repaidUnits, _position.debt);
}
```

This mirrors the intent of the `maxRepaid` guard in the normal-mode path and prevents the underflow while preserving the liquidator's collateral seizure. Alternatively, the bad-debt reduction and `repaidUnits` computation should use a consistent LIF value, or the subtraction at line 676 should be guarded with a `zeroFloorSub` paired with a cap on `repaidUnits`.

## Proof of Concept
Minimal Foundry test (add to `LiquidationTest.sol`):

```solidity
function testPostMaturityBadDebtSeizedAssetsReverts() public {
    // Setup: single collateral, price = ORACLE_PRICE_SCALE, maxLif = 2e18
    // debt = 452, collateral = 901
    // At price = ORACLE_PRICE_SCALE, maxLif = 2e18:
    //   collateral value / maxLif = 901/2 = 450 (floor) < 452 → bad debt = 1
    //   _position.debt after bad debt = 451
    // At Δt = 1s: lif ≈ WAD, repaidUnits = ceil(901 * WAD / lif) = 901 > 451 → revert

    // ... [setup market with maturity = block.timestamp - 1, collateral=901, debt=452, price=ORACLE_PRICE_SCALE, maxLif=2e18]
    vm.warp(market.maturity + 1); // Δt = 1 s into ramp
    vm.expectRevert(stdError.arithmeticError);
    midnight.liquidate(market, 0, 901, 0, borrower, true, address(this), address(0), "");
}
```

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

**File:** src/Midnight.sol (L676-676)
```text
            _position.debt -= UtilsLib.toUint128(repaidUnits);
```

**File:** src/libraries/ConstantsLib.sol (L19-19)
```text
uint256 constant TIME_TO_MAX_LIF = 15 minutes;
```

**File:** src/libraries/UtilsLib.sol (L38-42)
```text
    function toUint128(uint256 x) internal pure returns (uint128) {
        require(x <= type(uint128).max, CastOverflow());
        // forge-lint: disable-next-item(unsafe-typecast) as x is less than type(uint128).max
        return uint128(x);
    }
```
