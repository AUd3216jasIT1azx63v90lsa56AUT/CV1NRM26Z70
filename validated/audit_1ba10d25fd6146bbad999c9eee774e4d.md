### Title
Validator-Manipulable `block.timestamp` in Post-Maturity LIF Ramp Enables Excess Collateral Seizure — (File: src/Midnight.sol)

### Summary

`Midnight.sol` computes the post-maturity liquidation incentive factor (LIF) using `block.timestamp` directly over a `TIME_TO_MAX_LIF = 15 minutes` window. Because the entire LIF ramp from `WAD` to `maxLif` is compressed into 900 seconds, a validator who is also a liquidator (or colluding with one) can manipulate `block.timestamp` by ~12 seconds per block to extract a disproportionate share of a borrower's collateral. This is the direct analog of the HackerGold timestamp-dependence finding: a time-sensitive financial computation uses `block.timestamp` in a window narrow enough that validator-level timestamp drift is material.

### Finding Description

**Root cause — `src/Midnight.sol` line 646:**

```solidity
uint256 lif = postMaturityMode
    ? UtilsLib.min(_maxLif, WAD + (_maxLif - WAD) * (block.timestamp - market.maturity) / TIME_TO_MAX_LIF)
    : _maxLif;
``` [1](#0-0) 

`TIME_TO_MAX_LIF` is defined as exactly **15 minutes (900 seconds)**: [2](#0-1) 

The LIF linearly scales from `WAD` (1.0) at `market.maturity` to `maxLif` at `market.maturity + 900`. A validator can legally set `block.timestamp` up to ~12 seconds ahead of the true wall-clock time per Ethereum consensus rules. This 12-second drift represents `12 / 900 = 1.33%` of the entire LIF ramp.

**Exploit path:**

1. A large borrower's position becomes liquidatable at maturity (post-maturity mode).
2. A validator who is also a liquidator (or has a side-channel agreement with one) is scheduled to produce a block within the first 15 minutes after `market.maturity`.
3. The validator sets `block.timestamp = actual_time + 12` (within consensus-allowed range).
4. The liquidator calls `liquidate(..., postMaturityMode=true, ...)` in that block.
5. The inflated `lif` value is used to compute `seizedAssets`:

```solidity
seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
``` [3](#0-2) 

The liquidator seizes more collateral per unit repaid than the protocol intends at that moment.

**Secondary surface — settlement fee in `take()`:**

`timeToMaturity` is also derived from `block.timestamp` and fed into the piecewise-linear settlement fee: [4](#0-3) 

Near breakpoints (1d, 7d, 30d, 90d, 180d, 360d TTM), a validator can shift which interpolation segment is used, altering the fee charged to takers. The magnitude here is smaller than the LIF issue but follows the same root cause.

### Impact Explanation

For the lowest LLTV tier (`LLTV_0 = 0.385`) with `LIQUIDATION_CURSOR_HIGH = 0.5`:

```
maxLif = WAD / (WAD - 0.5 * (WAD - 0.385 * WAD))
       = 1e18 / 0.6925e18
       ≈ 1.4440e18
``` [5](#0-4) 

A 12-second timestamp manipulation at the midpoint of the ramp (t = maturity + 450s) yields:

```
Δlif = (1.4440e18 - 1e18) * 12 / 900 ≈ 0.00592e18
```

On a $10 M collateral position this translates to **~$59,200 of extra collateral** seized beyond what the protocol intends at that timestamp. The attack is silent, leaves no on-chain trace distinguishing it from a normal liquidation, and the borrower has no recourse.

### Likelihood Explanation

- Requires the attacker to be an Ethereum validator (or collude with one) — a realistic condition for any well-resourced liquidation bot operator.
- The attack window is the first 15 minutes after every market maturity. Markets are permissionlessly created, so many maturities will exist simultaneously.
- No privileged protocol role is needed; `liquidate` is permissionless (subject to optional `liquidatorGate`).
- The attack is profitable whenever `Δlif * collateralValue > validator_opportunity_cost`, which holds for any position above a few hundred thousand dollars.

### Recommendation

1. **Replace `block.timestamp` with `block.number` for the LIF ramp**, approximating `TIME_TO_MAX_LIF` in blocks (e.g., 75 blocks ≈ 15 minutes at 12 s/block). This removes sub-block timestamp drift from the financial computation.
2. Alternatively, **widen `TIME_TO_MAX_LIF`** significantly (e.g., 24 hours). A 12-second drift over 86,400 seconds is `0.014%` of the ramp — economically negligible.
3. For the settlement fee, the same fix applies: derive `timeToMaturity` from `block.number` rather than `block.timestamp`.

### Proof of Concept

**Setup:**
- Market: `loanToken = USDC`, `lltv = 0.385e18`, `maxLif ≈ 1.4440e18`, `maturity = T`.
- Borrower has `debt = 1_000_000` units, collateral worth $1.5 M at oracle price.
- Block is produced at real time `T + 450` (midpoint of ramp).

**Without manipulation:**
```
lif = 1e18 + (1.4440e18 - 1e18) * 450 / 900 = 1.2220e18
seizedAssets = repaidUnits * 1.2220e18 / 1e18 * ORACLE_PRICE_SCALE / price
```

**With validator setting `block.timestamp = T + 462` (+12 s):**
```
lif = 1e18 + (1.4440e18 - 1e18) * 462 / 900 = 1.2279e18
seizedAssets_manipulated = repaidUnits * 1.2279e18 / 1e18 * ORACLE_PRICE_SCALE / price
```

**Extra collateral seized per 1,000,000 repaid units:**
```
Δseized = 1_000_000 * (1.2279e18 - 1.2220e18) / 1e18
        = 1_000_000 * 0.0059
        = 5,900 collateral units
```

At a $1 collateral price this is **$5,900 extracted from the borrower** in a single liquidation call, with no protocol-level detection mechanism. [6](#0-5) [2](#0-1)

### Citations

**File:** src/Midnight.sol (L359-360)
```text
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
        uint256 _settlementFee = settlementFee(id, timeToMaturity);
```

**File:** src/Midnight.sol (L643-647)
```text
        if (repaidUnits > 0 || seizedAssets > 0) {
            uint256 _maxLif = market.collateralParams[collateralIndex].maxLif;
            uint256 lif = postMaturityMode
                ? UtilsLib.min(_maxLif, WAD + (_maxLif - WAD) * (block.timestamp - market.maturity) / TIME_TO_MAX_LIF)
                : _maxLif;
```

**File:** src/Midnight.sol (L652-652)
```text
                seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
```

**File:** src/libraries/ConstantsLib.sol (L19-19)
```text
uint256 constant TIME_TO_MAX_LIF = 15 minutes;
```

**File:** src/libraries/ConstantsLib.sol (L50-52)
```text
function maxLif(uint256 lltv, uint256 cursor) pure returns (uint256) {
    return UtilsLib.mulDivDown(WAD, WAD, WAD - UtilsLib.mulDivDown(cursor, WAD - lltv, WAD));
}
```
