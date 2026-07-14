### Title
Validator Timestamp Manipulation Inflates Post-Maturity LIF, Enabling Excess Collateral Seizure — (File: src/Midnight.sol)

---

### Summary

`Midnight.liquidate()` computes the post-maturity Liquidation Incentive Factor (LIF) using `block.timestamp` directly. Because `TIME_TO_MAX_LIF = 15 minutes`, the LIF ramp is extremely sensitive to small timestamp shifts. A validator who is also a liquidator can advance `block.timestamp` by ~15 seconds to claim a disproportionately higher LIF, seizing more collateral from a borrower than the protocol intends at that moment.

---

### Finding Description

In `liquidate()`, when `postMaturityMode == true`, the LIF is computed as:

```solidity
uint256 lif = postMaturityMode
    ? UtilsLib.min(_maxLif, WAD + (_maxLif - WAD) * (block.timestamp - market.maturity) / TIME_TO_MAX_LIF)
    : _maxLif;
``` [1](#0-0) 

`TIME_TO_MAX_LIF` is defined as:

```solidity
uint256 constant TIME_TO_MAX_LIF = 15 minutes;
``` [2](#0-1) 

The LIF ramps linearly from `WAD` (1.0) at `market.maturity` to `_maxLif` over exactly 900 seconds. Ethereum PoS validators can legally set `block.timestamp` up to ~15 seconds ahead of the parent block's timestamp. A 15-second forward shift represents `15 / 900 = 1/60 ≈ 1.67%` of the entire LIF ramp.

The seized collateral is computed as:

```solidity
seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
``` [3](#0-2) 

A higher `lif` directly increases `seizedAssets` for the same `repaidUnits`. The excess collateral comes entirely at the borrower's expense.

**Exploit path:**

1. Attacker is a validator (or cooperates with one via MEV relay) and also acts as a liquidator.
2. A market reaches maturity. A borrower's position becomes liquidatable in post-maturity mode.
3. The attacker waits until they are scheduled to propose a block shortly after maturity.
4. When proposing the block, the attacker sets `block.timestamp` to the maximum allowed value (~15 seconds ahead of the parent timestamp).
5. The attacker calls `liquidate(..., postMaturityMode=true, ...)` within the same block.
6. The inflated `block.timestamp` yields a higher `lif` than the real elapsed time warrants, so `seizedAssets` is larger than it should be.

---

### Impact Explanation

The extra collateral seized per unit of debt repaid scales with `(_maxLif - WAD)`. For the lowest allowed LLTV (0.385) with `LIQUIDATION_CURSOR_HIGH = 0.5`:

```
maxLif = WAD / (WAD - 0.5 * (WAD - 0.385 * WAD)) ≈ 1.444e18
extra_lif_from_15s = (0.444e18) * 15 / 900 ≈ 0.0074e18  (0.74%)
``` [4](#0-3) 

On a $1 million position, this yields ~$7,400 in excess collateral seized per block. The borrower receives less collateral back than the protocol's intended incentive schedule provides. The attack is repeatable across every post-maturity liquidation the attacker can front-run into their own block.

---

### Likelihood Explanation

- **Attacker preconditions**: Must control or cooperate with a block proposer (validator). No protocol-level privilege is required — this is a standard network-level capability.
- **Timing**: The attack is most profitable in the first 15 minutes after maturity, when the LIF ramp is steepest per second. This is also the period of highest liquidation activity.
- **MEV infrastructure**: MEV-boost and builder/relay networks make it routine for liquidation bots to target specific blocks. A validator running their own builder can trivially set the timestamp to the maximum allowed value.
- **Frequency**: Every market maturity event is a fresh opportunity.

---

### Recommendation

Replace the `block.timestamp`-based LIF ramp with a `block.number`-based equivalent, approximating `TIME_TO_MAX_LIF` as an expected block count (e.g., `TIME_TO_MAX_LIF_BLOCKS = 75` for 15 minutes at 12s/block). Store `maturityBlock` at market creation alongside `maturity`. Alternatively, accept the design tradeoff and increase `TIME_TO_MAX_LIF` substantially (e.g., to several hours), which reduces the percentage impact of any single timestamp manipulation to negligible levels.

---

### Proof of Concept

**Setup:**
- Market with `loanToken = USDC`, `lltv = 0.385e18`, `maxLif ≈ 1.444e18`, `maturity = T`.
- Borrower has `debt = 1,000,000 units`, collateral worth $1,500,000 at oracle price.

**Normal liquidation at `block.timestamp = T + 1` (1 second post-maturity):**
```
lif = WAD + (0.444e18) * 1 / 900 ≈ 1.000493e18
seizedAssets = 1_000_000 * 1.000493 / 1 * ORACLE_PRICE_SCALE / price
```

**Manipulated liquidation at `block.timestamp = T + 16` (validator sets +15s):**
```
lif = WAD + (0.444e18) * 16 / 900 ≈ 1.007893e18
seizedAssets = 1_000_000 * 1.007893 / 1 * ORACLE_PRICE_SCALE / price
```

**Delta:** `seizedAssets_manipulated - seizedAssets_normal ≈ 7,400 units` of extra collateral seized from the borrower, with no additional debt repaid. The attacker profits by the full delta; the borrower loses it. [5](#0-4)

### Citations

**File:** src/Midnight.sol (L620-647)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );

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

**File:** src/libraries/ConstantsLib.sol (L22-23)
```text
uint256 constant LIQUIDATION_CURSOR_LOW = 0.25e18;
uint256 constant LIQUIDATION_CURSOR_HIGH = 0.5e18;
```
