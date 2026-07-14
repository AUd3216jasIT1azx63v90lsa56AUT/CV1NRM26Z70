### Title
No Slippage Protection in `liquidate()` Exposes Liquidators to Oracle Price Manipulation and MEV Front-Running — (File: src/Midnight.sol)

---

### Summary

The `liquidate` function in `Midnight.sol` computes either `seizedAssets` or `repaidUnits` at execution time using a live oracle price, with no caller-supplied minimum/maximum bound. An adversary can front-run a liquidation transaction or manipulate the oracle price between submission and execution, causing the liquidator to receive fewer collateral tokens than expected (or pay more debt units than expected), with no on-chain protection.

---

### Finding Description

**Root cause:** The oracle price for the liquidated collateral is fetched at execution time inside `liquidate`: [1](#0-0) 

This price is then used to compute the complementary quantity the liquidator did not specify: [2](#0-1) 

The function signature accepts no slippage-guard parameter: [3](#0-2) 

**Two attack paths, same root cause:**

**Path A — Liquidator specifies `repaidUnits` (seizedAssets = 0):**
`seizedAssets` is derived as:
```
seizedAssets = repaidUnits * lif / WAD * ORACLE_PRICE_SCALE / liquidatedCollatPrice
```
If `liquidatedCollatPrice` rises between submission and execution, `seizedAssets` shrinks. The liquidator pays the same debt but receives fewer collateral tokens.

**Path B — Liquidator specifies `seizedAssets` (repaidUnits = 0):**
`repaidUnits` is derived as:
```
repaidUnits = seizedAssets * liquidatedCollatPrice / ORACLE_PRICE_SCALE * WAD / lif
```
If `liquidatedCollatPrice` rises between submission and execution, `repaidUnits` grows. The liquidator receives the same collateral but must pay more debt units.

In both paths, there is no `minimumSeizedAssets` or `maximumRepaidUnits` guard to revert the transaction when the outcome is worse than the liquidator's expectation.

---

### Impact Explanation

A liquidator who submits a transaction expecting a profitable liquidation (e.g., seizing collateral worth more than the debt repaid) can instead execute at a loss if the oracle price moves adversarially. The liquidator:
- Pays real loan tokens (`repaidUnits` pulled at line 717)
- Receives fewer collateral tokens than anticipated (or pays more than anticipated) [4](#0-3) 

The loss is bounded by the magnitude of the oracle price move, but in volatile markets or under flash-loan-assisted oracle manipulation, this can be significant. Repeated exploitation discourages liquidators from participating, degrading protocol health and enabling bad debt accumulation.

---

### Likelihood Explanation

- **MEV front-running:** On any EVM chain with a public mempool, a searcher can observe a pending `liquidate` call, sandwich it with oracle-moving trades (or simply reorder it after a price-moving transaction), causing the liquidator to execute at a worse price. No privileged access is required.
- **Oracle manipulation:** `SECURITY.md` explicitly states oracle manipulation/flash-loan attacks are **not** excluded from scope. A flash loan can temporarily move the oracle price, causing the liquidation to execute at the manipulated price.
- **Natural price volatility:** Even without active manipulation, oracle prices can update between mempool submission and block inclusion, especially on chains with frequent oracle updates.

---

### Recommendation

Add a caller-supplied slippage guard to `liquidate`. Depending on which input is non-zero:

- When `repaidUnits > 0`: add `uint256 minimumSeizedAssets` and revert if `seizedAssets < minimumSeizedAssets`.
- When `seizedAssets > 0`: add `uint256 maximumRepaidUnits` and revert if `repaidUnits > maximumRepaidUnits`.

A unified approach is to add a single `uint256 slippageLimit` parameter whose interpretation depends on which of `seizedAssets`/`repaidUnits` is non-zero, and add the check immediately after lines 649–653:

```solidity
if (seizedAssets > 0) {
    repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
    require(repaidUnits <= slippageLimit, SlippageExceeded()); // maximumRepaidUnits
} else {
    seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
    require(seizedAssets >= slippageLimit, SlippageExceeded()); // minimumSeizedAssets
}
```

---

### Proof of Concept

**Setup:**
- Market with collateral token C and loan token L.
- Oracle reports price P = 1000 (collateral worth 1000 loan tokens per unit).
- Borrower has 1 unit of debt and 0.0011 units of collateral (position is unhealthy).
- Liquidator submits `liquidate(..., repaidUnits = 1, seizedAssets = 0, ...)` expecting to receive `seizedAssets = 1 * lif * ORACLE_PRICE_SCALE / 1000`.

**Attack:**
1. MEV bot observes the pending liquidation in the mempool.
2. Bot front-runs with a flash-loan-assisted trade that pushes the oracle price to P' = 1100.
3. Liquidator's transaction executes with `liquidatedCollatPrice = 1100`.
4. `seizedAssets = repaidUnits * lif * ORACLE_PRICE_SCALE / 1100` — approximately 9% fewer collateral tokens than expected.
5. Liquidator pays 1 unit of debt but receives ~9% less collateral, turning a profitable liquidation into a loss or near-zero-profit operation.

The bot profits from the price difference; the liquidator bears the loss with no on-chain recourse. [2](#0-1) [4](#0-3)

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

**File:** src/Midnight.sol (L609-611)
```text
            CollateralParams memory _collateralParam = market.collateralParams[i];
            uint256 price = IOracle(_collateralParam.oracle).price();
            if (i == collateralIndex) liquidatedCollatPrice = price;
```

**File:** src/Midnight.sol (L649-653)
```text
            if (seizedAssets > 0) {
                repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
            } else {
                seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
            }
```

**File:** src/Midnight.sol (L696-717)
```text
        SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);

        if (callback != address(0)) {
            require(
                ILiquidateCallback(callback)
                    .onLiquidate(
                        msg.sender,
                        id,
                        market,
                        collateralIndex,
                        seizedAssets,
                        repaidUnits,
                        borrower,
                        receiver,
                        data,
                        badDebt
                    ) == CALLBACK_SUCCESS,
                WrongLiquidateCallbackReturnValue()
            );
        }

        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
```
