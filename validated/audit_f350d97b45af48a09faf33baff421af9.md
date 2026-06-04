### Title
L2 Sequencer Downtime Causes Post-Maturity LIF to Reach Maximum, Enabling Excess Collateral Seizure — (`src/Midnight.sol`)

### Summary

The post-maturity liquidation incentive factor (LIF) in `Midnight.sol` ramps linearly from `WAD` (1.0) to `maxLif` over `TIME_TO_MAX_LIF = 15 minutes`. This ramp is a Dutch auction for liquidators. When deployed on an L2 and the sequencer goes offline, `block.timestamp` continues to advance (derived from L1), but no transactions can be processed. Borrowers cannot repay and liquidators cannot act. When the sequencer comes back online, the LIF has already reached its maximum, allowing liquidators to seize the maximum possible collateral — far more than they would have been entitled to had the sequencer been online.

---

### Finding Description

**Root cause — `src/Midnight.sol`, `liquidate()`, lines 645–647:**

```solidity
uint256 lif = postMaturityMode
    ? UtilsLib.min(_maxLif, WAD + (_maxLif - WAD) * (block.timestamp - market.maturity) / TIME_TO_MAX_LIF)
    : _maxLif;
``` [1](#0-0) 

`TIME_TO_MAX_LIF` is hardcoded to **15 minutes**: [2](#0-1) 

There is no sequencer uptime check anywhere in `Midnight.sol` or in the oracle interface (`IOracle.price()`). The protocol blindly trusts `block.timestamp` to reflect real elapsed time during which liquidations were possible.

**Exploit flow:**

1. A market reaches `maturity` on an L2 (Arbitrum, Optimism, Base, etc.).
2. The L2 sequencer goes offline. On L2s, `block.timestamp` is derived from L1 and continues to advance even when the sequencer is down — no L2 blocks are produced, so no transactions can execute.
3. During the outage, the LIF ramp `WAD + (_maxLif - WAD) * elapsed / 15 minutes` advances toward `maxLif` with zero liquidations possible.
4. Borrowers who intended to repay immediately after maturity cannot submit transactions.
5. After ≥ 15 minutes of downtime, `lif = maxLif` for all subsequent liquidations.
6. When the sequencer comes back online, liquidators immediately call `liquidate()` with `postMaturityMode = true` and receive `maxLif` collateral per unit of debt repaid — the maximum possible seizure — even though the market had been live for only seconds before the outage.

**Collateral seizure calculation (lines 649–653):**

```solidity
if (seizedAssets > 0) {
    repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
} else {
    seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
}
``` [3](#0-2) 

A higher `lif` means more `seizedAssets` per `repaidUnits`. At `maxLif`, the liquidator extracts the maximum collateral bonus.

---

### Impact Explanation

Borrowers suffer excess collateral seizure beyond what the protocol's Dutch auction design intended. For example, with `LLTV = 0.86` and `LIQUIDATION_CURSOR_HIGH = 0.5`:

```
maxLif = WAD / (WAD - 0.5 * (WAD - 0.86 * WAD)) = 1 / (1 - 0.07) ≈ 1.0753
``` [4](#0-3) 

A borrower who could have been liquidated at LIF = 1.01 (10 seconds post-maturity) instead faces LIF = 1.0753 — a ~6.5% excess collateral loss — purely because the sequencer was down for 15 minutes. This is a direct, quantifiable loss of user funds with no attacker-controlled input required beyond timing the liquidation call after sequencer recovery.

---

### Likelihood Explanation

L2 sequencer outages are documented real events (Arbitrum experienced a ~7-hour outage in 2023; Optimism has had multiple shorter outages). The `TIME_TO_MAX_LIF = 15 minutes` window is extremely tight — any outage exceeding 15 minutes after a market's maturity is sufficient to push LIF to its maximum. No privileged access is required; any liquidator can exploit this by simply waiting for sequencer recovery and calling `liquidate()`.

---

### Recommendation

Before computing the post-maturity LIF, verify that the L2 sequencer was live during the elapsed period using Chainlink's L2 sequencer uptime feed. If the sequencer was down at any point during `[market.maturity, block.timestamp]`, either:

1. Revert the liquidation, or
2. Cap the effective elapsed time to exclude the downtime period, so the LIF reflects only the time during which liquidations were actually possible.

Example check pattern (Chainlink sequencer feed):
```solidity
(, int256 answer, uint256 startedAt,,) = sequencerUptimeFeed.latestRoundData();
require(answer == 0, "Sequencer is down");
require(block.timestamp - startedAt > GRACE_PERIOD, "Grace period not elapsed");
```

---

### Proof of Concept

**Setup:** Deploy `Midnight.sol` on a forked L2 (e.g., Arbitrum). Create a market with `maturity = T`.

1. Warp to `T` (maturity).
2. Simulate sequencer downtime by warping `block.timestamp` forward by 15 minutes without processing any transactions (no L2 blocks produced).
3. Warp to `T + 15 minutes + 1 second`.
4. Call `liquidate(market, collateralIndex, 0, repaidUnits, borrower, true, receiver, address(0), "")`.
5. Observe: `lif = maxLif` (the full ramp has elapsed), and `seizedAssets` equals the maximum possible for the given `repaidUnits`.
6. Compare: if the same call were made at `T + 1 second` (sequencer online), `lif ≈ WAD` and `seizedAssets` would be ~7% less.

The borrower loses the difference in seized collateral — a direct fund loss caused solely by sequencer downtime and the absence of any uptime guard in `liquidate()`. [5](#0-4)

### Citations

**File:** src/Midnight.sol (L581-647)
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
        bytes32 id = touchMarket(market);
        MarketState storage _marketState = marketState[id];
        Position storage _position = position[id][borrower];
        require(UtilsLib.atMostOneNonZero(repaidUnits, seizedAssets), InconsistentInput());
        require(_position.debt > 0, NotBorrower()); // to avoid no-op liquidations of non borrower positions.
        require(
            market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
            LiquidatorGatedFromLiquidating()
        );

        uint256 maxDebt;
        uint256 liquidatedCollatPrice;
        uint256 originalDebt = _position.debt;
        uint256 badDebt = originalDebt;
        uint128 _collateralBitmap = _position.collateralBitmap;
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

**File:** src/Midnight.sol (L649-653)
```text
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

**File:** src/libraries/ConstantsLib.sol (L50-52)
```text
function maxLif(uint256 lltv, uint256 cursor) pure returns (uint256) {
    return UtilsLib.mulDivDown(WAD, WAD, WAD - UtilsLib.mulDivDown(cursor, WAD - lltv, WAD));
}
```
