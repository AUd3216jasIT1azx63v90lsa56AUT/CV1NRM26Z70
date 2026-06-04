### Title
Oracle Price Frontrunning in `liquidate()` Allows Liquidators to Seize Excess Collateral at Borrower's Expense — (File: `src/Midnight.sol`)

---

### Summary

The `liquidate()` function in `Midnight.sol` fetches the collateral oracle price at execution time and uses it directly to compute the exchange rate between repaid debt units and seized collateral. A liquidator who observes that the real market price of collateral is rising — and that the oracle has not yet updated — can frontrun the pending oracle update transaction to liquidate at the stale (lower) price, seizing more collateral than the fair-value exchange rate would allow. This is the same vulnerability class as DYAD H-03 (oracle-price-based exchange rate exploitable via frontrunning), applied to liquidations instead of deposits/redemptions.

---

### Finding Description

**Root cause — spot oracle price used as liquidation exchange rate**

Inside `liquidate()`, the oracle is called once per activated collateral to determine both the health of the borrower and the collateral-to-debt exchange rate used to compute seized assets:

```solidity
// src/Midnight.sol  lines 607-618
while (_collateralBitmap != 0) {
    uint256 i = UtilsLib.msb(_collateralBitmap);
    CollateralParams memory _collateralParam = market.collateralParams[i];
    uint256 price = IOracle(_collateralParam.oracle).price();          // ← spot price
    if (i == collateralIndex) liquidatedCollatPrice = price;           // ← stored for exchange rate
    uint256 _collateral = _position.collateral[i];
    maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE)
                           .mulDivDown(_collateralParam.lltv, WAD);
    ...
}
``` [1](#0-0) 

That same `liquidatedCollatPrice` is then used to compute how much collateral the liquidator receives:

```solidity
// src/Midnight.sol  lines 649-652
} else {
    seizedAssets = repaidUnits.mulDivDown(lif, WAD)
                              .mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
}
``` [2](#0-1) 

Because `seizedAssets` is inversely proportional to `liquidatedCollatPrice`, a **lower** oracle price yields **more** collateral for the same repaid debt. There is no TWAP, no staleness guard, and no delay — the spot price at the block of the `liquidate()` call is the only input.

**Exploit path**

1. A borrower is unhealthy at the current (stale) oracle price `P_old` but would be healthy at the real market price `P_new > P_old` (e.g., the oracle has a 0.5 % deviation threshold and the asset just crossed it).
2. The attacker watches the mempool and sees the oracle's `transmit()` / `updateAnswer()` transaction.
3. The attacker submits `liquidate()` with a higher gas price, executing **before** the oracle update.
4. At `P_old`, `seizedAssets = repaidUnits × lif × ORACLE_PRICE_SCALE / P_old` — more collateral than at `P_new`.
5. After the oracle updates to `P_new`, the seized collateral is worth `seizedAssets × P_new / ORACLE_PRICE_SCALE` in loan-token terms — strictly more than what was repaid plus the intended LIF incentive.
6. The borrower loses excess collateral; the attacker pockets the difference.

The `liquidate()` function is fully permissionless:

```solidity
// src/Midnight.sol  lines 581-591
function liquidate(
    Market calldata market,
    uint256 collateralIndex,
    uint256 seizedAssets,
    uint256 repaidUnits,
    address borrower,
    ...
) external returns (uint256, uint256) {
``` [3](#0-2) 

The only gate is the optional `liquidatorGate`, which is `address(0)` (unrestricted) by default. [4](#0-3) 

The Recovery Close Factor (RCF) does **not** mitigate this: it is computed using the same stale `liquidatedCollatPrice`, so it caps `maxRepaid` relative to the stale price, not the real price. [5](#0-4) 

---

### Impact Explanation

A borrower who is unhealthy only because the oracle lags the real market price is liquidated at a worse exchange rate than they would receive once the oracle catches up. The attacker seizes collateral worth `(P_new − P_old) / P_old × seizedAssets` more than the fair-value LIF-adjusted amount. With a 0.5 % oracle deviation band and a large position, this is a direct, repeatable theft of borrower collateral. The loss is permanent — there is no recovery mechanism.

---

### Likelihood Explanation

- Chainlink and most production oracles use a deviation threshold (typically 0.5 %–1 %) before updating. The window between the real price crossing the threshold and the oracle transaction confirming is predictable and observable.
- Frontrunning oracle update transactions is a well-known MEV strategy; searchers already do this on mainnet.
- No privileged access is required. Any EOA or contract can call `liquidate()`.
- The attack is profitable whenever the price movement exceeds gas costs, which is easily satisfied for positions of meaningful size.

---

### Recommendation

Replace the spot oracle call with a mechanism that removes the frontrunning advantage:

1. **TWAP oracle**: Require oracles to expose a time-weighted average price over a window (e.g., 30 minutes). This makes the price resistant to single-block manipulation and reduces the frontrunning window.
2. **Liquidation delay / commit-reveal**: Record a liquidation intent in one transaction and execute seizure in a later block, after the oracle has had time to update.
3. **Price freshness check**: Require that the oracle price was updated within the current block or within a short staleness window, so liquidators cannot exploit a price that is known to be stale.

---

### Proof of Concept

**Setup:**
- Market: ETH collateral, USDC loan token, LLTV = 0.945 (`LLTV_5`), `maxLif` = `maxLif(0.945e18, 0.5e18)` ≈ 1.099
- Borrower: 1 ETH collateral, 940 USDC debt
- Oracle price `P_old` = 1000e36 (i.e., $1,000 per ETH in `ORACLE_PRICE_SCALE` units)
- `maxDebt` at `P_old` = 1 × 1000 × 0.945 = 945 → borrower is **unhealthy** (940 < 945, wait — let me use debt = 946 to be clearly unhealthy)

**Concrete numbers:**
- debt = 946, maxDebt = 945 → unhealthy by 1 unit
- Real ETH price = $1,010; oracle will update to `P_new` = 1010e36
- At `P_new`: maxDebt = 1010 × 0.945 = 954.45 → borrower would be **healthy**

**Attack:**
1. Attacker sees oracle `transmit()` in mempool (price update to $1,010).
2. Attacker frontrunning with higher gas, calls `liquidate(repaidUnits = 946)`.
3. At `P_old = 1000e36`:
   - `seizedAssets = 946 × 1.099 × 1e36 / 1000e36 ≈ 1.0397 ETH`
4. Oracle updates to `P_new = 1010e36`.
5. Attacker's 1.0397 ETH is now worth `1.0397 × 1010 = $1,050` in USDC.
6. Attacker repaid 946 USDC and received collateral worth $1,050 — a profit of ~$104 above the intended LIF incentive (~$99.7), at the borrower's expense.
7. The borrower, who would have been healthy at the new price, lost their entire collateral position. [6](#0-5) [2](#0-1)

### Citations

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

**File:** src/Midnight.sol (L597-600)
```text
        require(
            market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
            LiquidatorGatedFromLiquidating()
        );
```

**File:** src/Midnight.sol (L607-618)
```text
        while (_collateralBitmap != 0) {
            uint256 i = UtilsLib.msb(_collateralBitmap);
            CollateralParams memory _collateralParam = market.collateralParams[i];
            uint256 price = IOracle(_collateralParam.oracle).price();
            if (i == collateralIndex) liquidatedCollatPrice = price;
            uint256 _collateral = _position.collateral[i];
            maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(_collateralParam.lltv, WAD);
            badDebt = badDebt.zeroFloorSub(
                _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
            );
            _collateralBitmap = _collateralBitmap.clearBit(i);
        }
```

**File:** src/Midnight.sol (L649-652)
```text
            if (seizedAssets > 0) {
                repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
            } else {
                seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
```

**File:** src/Midnight.sol (L655-667)
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
```
