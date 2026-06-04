### Title
`liquidate` in Post-Maturity Mode Lacks Slippage Protection for Liquidators — (File: src/Midnight.sol)

---

### Summary

The `liquidate` function in `Midnight.sol` computes the Liquidation Incentive Factor (LIF) dynamically from `block.timestamp` in post-maturity mode. Because no minimum-LIF or maximum-repaid-units bound is accepted as input, a chain reorg that shifts the transaction to an earlier block silently worsens the liquidator's execution price with no on-chain protection.

---

### Finding Description

**Root cause — time-dependent LIF with no caller-supplied bound**

In post-maturity mode the LIF grows linearly from `WAD` (1.0) at `market.maturity` to `maxLif` at `market.maturity + TIME_TO_MAX_LIF` (15 minutes): [1](#0-0) [2](#0-1) 

The LIF feeds directly into the seized/repaid conversion:

- **`seizedAssets` input path** — `repaidUnits` is computed as `seizedAssets * price / lif`. A lower LIF → higher `repaidUnits` → liquidator pays more loan tokens than expected. [3](#0-2) 

- **`repaidUnits` input path** — `seizedAssets` is computed as `repaidUnits * lif / price`. A lower LIF → fewer collateral tokens received → liquidator receives less than expected. [4](#0-3) 

The `liquidate` function signature accepts no `minSeizedAssets`, `maxRepaidUnits`, or `minLif` parameter: [5](#0-4) 

By contrast, the `take` function explicitly acknowledges the analogous risk and recommends wrapping in a smart contract for atomic price checks: [6](#0-5) 

No equivalent warning or mitigation exists for `liquidate`.

---

### Impact Explanation

A liquidator submitting a post-maturity liquidation transaction at time `T` (expecting LIF `L`) can have that transaction re-executed at time `T' < T` after a reorg, where LIF `L' < L`. The liquidator:

- Overpays loan tokens (when specifying `seizedAssets`), or
- Receives fewer collateral tokens (when specifying `repaidUnits`),

with no on-chain check to revert the transaction. The loss scales with the LIF delta: across the full 15-minute ramp, LIF moves from 1.0 to `maxLif` (e.g., ~1.15 for LLTV = 0.86 with high cursor). A reorg of even 1–2 minutes can shift LIF by several percent, causing meaningful financial loss on large liquidations.

---

### Likelihood Explanation

The protocol is explicitly designed for deployment on any EVM-compatible chain: [7](#0-6) 

Many such chains (Polygon, BSC, Avalanche, Optimism pre-Bedrock, etc.) experience multi-block reorgs regularly. The 15-minute LIF ramp window (`TIME_TO_MAX_LIF`) is short enough that even modest reorgs (seconds to a few minutes) produce a measurable LIF shift. No privileged access is required; any liquidator is exposed.

---

### Recommendation

Add a caller-supplied slippage bound to `liquidate`:

- A `minSeizedAssets` parameter checked after computing `seizedAssets` when `repaidUnits` is the input, or
- A `maxRepaidUnits` parameter checked after computing `repaidUnits` when `seizedAssets` is the input.

This mirrors the fix applied to the analogous kairos vulnerability (PR #50) and is consistent with the pattern already recommended for `take` via atomic smart-contract wrappers.

---

### Proof of Concept

**Setup:**
- Market maturity = `T0`. `TIME_TO_MAX_LIF` = 900 s. `maxLif` = 1.15e18 (LLTV = 0.86, high cursor).
- Borrower has 1000 collateral tokens at oracle price = 1e36 (1:1 with loan token).
- Borrower's debt = 1000 units (post-maturity, fully liquidatable).

**Liquidator's intent (at T0 + 450 s, LIF ≈ 1.075):**
- Calls `liquidate(..., seizedAssets=100, repaidUnits=0, ..., postMaturityMode=true)`
- Expected `repaidUnits` = `100 * 1e36 / 1e36 / 1.075` ≈ **93 units**

**After reorg (tx lands at T0 + 60 s, LIF ≈ 1.01):**
- Actual `repaidUnits` = `100 * 1e36 / 1e36 / 1.01` ≈ **99 units**
- Liquidator is charged **~6 extra loan tokens** per 100 collateral seized, with no revert.

The liquidator has no on-chain recourse because `liquidate` accepts no slippage bound. [8](#0-7)

### Citations

**File:** src/Midnight.sol (L329-331)
```text
    /// @dev The taker might not get the price they expected if the settlement fee was just changed. A smart-contract
    /// can be used to perform atomic price checks.
    /// @dev Taking buy offers with price < settlement fee will revert.
```

**File:** src/Midnight.sol (L581-591)
```text
    function liquidate(
        Market calldata market,
        uint256 collateralIndex,
        uint256 seizedAssets,
        uint256 repaidUnits,
        address borrower,
        bool postMaturityMode,
        address receiver,
        address callback,
        bytes calldata data
    ) external returns (uint256, uint256) {
```

**File:** src/Midnight.sol (L643-653)
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
```

**File:** src/libraries/ConstantsLib.sol (L19-19)
```text
uint256 constant TIME_TO_MAX_LIF = 15 minutes;
```

**File:** README.md (L1-4)
```markdown
# Morpho Midnight

Morpho Midnight is a non-custodial fixed-rate lending protocol implemented for the Ethereum Virtual Machine.
It is organized around isolated, immutable, permissionlessly created markets with fixed-maturity.
```
