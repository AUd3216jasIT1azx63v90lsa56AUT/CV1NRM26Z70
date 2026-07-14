### Title
Validator `block.timestamp` Manipulation Enables Premature Post-Maturity Liquidation of Healthy Borrowers — (File: src/Midnight.sol)

---

### Summary

`Midnight.liquidate()` gates post-maturity mode and computes the Liquidation Incentive Factor (LIF) entirely from `block.timestamp`. The constant `TIME_TO_MAX_LIF = 15 minutes` makes the LIF extremely sensitive to small timestamp shifts. A validator who is also a liquidator can advance `block.timestamp` by ~12 seconds (one Ethereum PoS slot) to cross the maturity boundary, enabling post-maturity liquidation of healthy borrowers before the market has actually matured, and extracting inflated collateral via an artificially elevated LIF.

---

### Finding Description

**Root cause — two timestamp-dependent checks in `liquidate()`:**

**Check 1 — maturity gate** (`src/Midnight.sol` line 622):
```solidity
postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt
```
Post-maturity mode is unlocked the instant `block.timestamp` exceeds `market.maturity`. A validator can set their block's timestamp up to ~12 seconds ahead of the canonical slot time, crossing this boundary before the market has actually matured.

**Check 2 — LIF computation** (`src/Midnight.sol` line 646):
```solidity
uint256 lif = postMaturityMode
    ? UtilsLib.min(_maxLif, WAD + (_maxLif - WAD) * (block.timestamp - market.maturity) / TIME_TO_MAX_LIF)
    : _maxLif;
```
With `TIME_TO_MAX_LIF = 15 minutes = 900 seconds` (`src/libraries/ConstantsLib.sol` line 19), a 12-second timestamp advance yields:

```
lif = WAD + (maxLif - WAD) * 12 / 900
```

For LLTV = 0.385 with `LIQUIDATION_CURSOR_HIGH`:
- `maxLif ≈ 1.444e18`
- LIF at +12 s ≈ `1.00592e18` → liquidator seizes **0.592% more collateral** than debt repaid

For LLTV = 0.77 with `LIQUIDATION_CURSOR_HIGH`:
- `maxLif ≈ 1.130e18`
- LIF at +12 s ≈ `1.00173e18` → **0.173% extra collateral**

**Why this is worse than a normal liquidation:**

In normal mode, the Recovery Close Factor (RCF) limits how much of a position can be liquidated in one call. Post-maturity mode **deactivates the RCF entirely** (line 655: `if (!postMaturityMode) { ... maxRepaid check ... }`). So the attacker can liquidate the borrower's entire position in one transaction, not just the portion needed to restore health.

**Exploit flow:**

1. Attacker monitors a market approaching `market.maturity` with healthy borrowers (debt < maxDebt).
2. Attacker is assigned a validator slot within ~12 seconds before `market.maturity`.
3. Attacker sets `block.timestamp = market.maturity + ε` (e.g., +12 s).
4. Attacker calls `liquidate(..., postMaturityMode = true, ...)`.
5. The check `block.timestamp > market.maturity` passes.
6. RCF is skipped; the full position is liquidatable.
7. LIF is `WAD + (maxLif - WAD) * 12 / 900` — attacker seizes more collateral than the debt they repay. [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

- **Healthy borrowers are liquidated before actual maturity.** A borrower with debt < maxDebt (fully healthy) can have their entire position seized because post-maturity mode disables the RCF.
- **Collateral theft via inflated LIF.** The liquidator seizes up to 0.592% more collateral than the debt they repay (for low-LLTV markets). On a $10M position this is ~$59,200 per exploit.
- **Bad debt socialization triggered prematurely.** If the borrower has no collateral left after the liquidation, bad debt is socialized among all lenders in the market, harming uninvolved parties. [4](#0-3) 

---

### Likelihood Explanation

- **Attacker profile:** A validator who also controls a liquidation bot. This is realistic — MEV searchers routinely run validators.
- **Trigger condition:** The attacker must be assigned a slot within ~12 seconds before `market.maturity`. Given that Ethereum produces ~7,200 slots/day, and an attacker controlling even 1% of stake gets ~72 slots/day, the probability of landing a slot in the 12-second window before any given maturity is non-trivial for a patient attacker who can choose market maturities.
- **No special permissions required.** `liquidate()` is permissionless (subject only to `liquidatorGate`, which defaults to `address(0)`).
- **Profitability scales with position size**, making large markets high-value targets. [5](#0-4) 

---

### Recommendation

Replace the raw `block.timestamp` comparison in the maturity gate with a grace-period buffer to absorb validator timestamp drift:

```solidity
// Instead of:
postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt

// Use a buffer (e.g., 60 seconds):
postMaturityMode ? block.timestamp > market.maturity + MATURITY_BUFFER : originalDebt > maxDebt
```

Similarly, for the LIF computation, clamp `block.timestamp - market.maturity` to start only after the buffer:

```solidity
uint256 elapsed = block.timestamp > market.maturity + MATURITY_BUFFER
    ? block.timestamp - market.maturity - MATURITY_BUFFER
    : 0;
uint256 lif = postMaturityMode
    ? UtilsLib.min(_maxLif, WAD + (_maxLif - WAD) * elapsed / TIME_TO_MAX_LIF)
    : _maxLif;
```

A `MATURITY_BUFFER` of 60 seconds (5 slots) eliminates the practical manipulation window while adding negligible delay for legitimate liquidators. [3](#0-2) 

---

### Proof of Concept

**Setup:**
- Market with `maturity = T`, LLTV = 0.77, `maxLif ≈ 1.130e18`, `TIME_TO_MAX_LIF = 900s`
- Borrower has `debt = 900_000e18`, `maxDebt = 950_000e18` (healthy)
- Attacker is a validator assigned slot at `T - 10s`

**Steps:**
1. Attacker's validator sets `block.timestamp = T + 12` (12 seconds past maturity).
2. Attacker calls:
   ```solidity
   midnight.liquidate(
       market,
       collateralIndex,
       0,                  // seizedAssets = 0 (use repaidUnits path)
       900_000e18,         // repaidUnits = full debt
       borrower,
       true,               // postMaturityMode = true
       attacker,
       address(0),
       ""
   );
   ```
3. Line 622: `block.timestamp (T+12) > market.maturity (T)` → passes.
4. Line 646: `lif = 1e18 + 0.130e18 * 12/900 = 1.001733e18`
5. Line 652: `seizedAssets = 900_000e18 * 1.001733e18 / 1e18 * ORACLE_PRICE_SCALE / price`
   → Attacker seizes **1,560 extra units of collateral** (0.173% of 900,000) beyond fair value.
6. RCF check is skipped (post-maturity mode), so the full position is liquidated in one call.
7. Borrower loses their entire collateral position despite being healthy at the true wall-clock time. [1](#0-0) [6](#0-5) [3](#0-2)

### Citations

**File:** src/Midnight.sol (L591-600)
```text
    ) external returns (uint256, uint256) {
        bytes32 id = touchMarket(market);
        MarketState storage _marketState = marketState[id];
        Position storage _position = position[id][borrower];
        require(UtilsLib.atMostOneNonZero(repaidUnits, seizedAssets), InconsistentInput());
        require(_position.debt > 0, NotBorrower()); // to avoid no-op liquidations of non borrower positions.
        require(
            market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
            LiquidatorGatedFromLiquidating()
        );
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
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

**File:** src/libraries/ConstantsLib.sol (L19-19)
```text
uint256 constant TIME_TO_MAX_LIF = 15 minutes;
```
