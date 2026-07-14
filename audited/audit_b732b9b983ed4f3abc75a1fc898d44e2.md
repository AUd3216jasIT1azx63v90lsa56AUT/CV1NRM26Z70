### Title
Spot Oracle Price Manipulation via Flash Loan Enables Liquidation of Healthy Positions — (File: src/Midnight.sol)

---

### Summary

The `liquidate()` function in `src/Midnight.sol` fetches a raw spot price from `IOracle.price()` with no time-weighted average or manipulation-resistance check enforced at the protocol level. An attacker can use a flash loan to temporarily depress the collateral's spot price, making a healthy borrower's position appear liquidatable, then execute the liquidation at the manipulated price to seize more collateral than the fair-value exchange rate warrants.

---

### Finding Description

Both `liquidate()` and `isHealthy()` call `IOracle(oracle).price()` directly: [1](#0-0) [2](#0-1) 

The returned `price` value serves two roles inside `liquidate()`:

1. **Liquidatability gate** — `maxDebt` is computed from `price` and compared against `originalDebt` at line 622. If `price` is artificially depressed, `maxDebt` falls below `originalDebt`, making a healthy position appear liquidatable. [3](#0-2) 

2. **Collateral exchange rate** — `liquidatedCollatPrice` (set from the same `price` at line 611) determines how many collateral tokens the liquidator seizes per repaid unit: [4](#0-3) 

A lower `liquidatedCollatPrice` means `seizedAssets = repaidUnits * lif / WAD * ORACLE_PRICE_SCALE / liquidatedCollatPrice` yields **more collateral** for the same debt repaid. The protocol enforces no TWAP, no minimum observation window, and no slippage bound on the oracle return value.

The `withdrawCollateral()` path is also affected: it calls `isHealthy()` which uses the same spot price, so a manipulated price upward could allow a borrower to withdraw collateral that should be locked. [5](#0-4) 

---

### Impact Explanation

A healthy borrower can be liquidated against their will. The attacker repays a portion of the borrower's debt and seizes collateral at a price below true market value, pocketing the spread after repaying the flash loan. The borrower suffers an unwarranted loss of collateral. In markets with large positions and low-liquidity AMM-based oracles, the profit can be substantial and the borrower's loss is permanent.

---

### Likelihood Explanation

`liquidate()` is a permissionless function — no privileged role is required. The attack requires only that the market's oracle derives its price from a manipulable on-chain source (e.g., a Uniswap V3 slot0 price or any single-block spot price). Such oracles are common in DeFi deployments. Flash loans make the capital requirement effectively zero. `SECURITY.md` explicitly states: *"This does not exclude oracle manipulation/flash-loan attacks."* [6](#0-5) 

---

### Recommendation

The protocol should document and enforce that all oracles supplied to `CollateralParams.oracle` must return a manipulation-resistant price (e.g., a Uniswap V3 TWAP over a sufficient observation window, a Chainlink aggregator, or a similar time-averaged feed). Optionally, `liquidate()` could accept a caller-supplied minimum price bound and revert if `liquidatedCollatPrice` falls below it, giving borrowers and liquidators a slippage guard analogous to the recommendation in the external report.

---

### Proof of Concept

**Setup:**
- Market: loanToken = USDC, collateral = WETH, LLTV = 80 %, oracle = Uniswap V3 spot price
- Borrower state: 100 WETH collateral, 70,000 USDC debt
- Fair ETH price: $1,000 → `maxDebt` = 100 × 1000 × 0.8 = 80,000 USDC > 70,000 → **healthy**

**Attack steps:**

1. Attacker takes a flash loan of a large WETH amount.
2. Attacker dumps WETH into the Uniswap V3 pool, pushing the spot price to $800.
3. At $800: `maxDebt` = 100 × 800 × 0.8 = 64,000 USDC < 70,000 → **liquidatable** per line 622.
4. Attacker calls `liquidate(market, collateralIndex, 0, 70_000e6, borrower, false, attacker, address(0), "")`.
5. `seizedAssets = 70_000e6 * lif / WAD * ORACLE_PRICE_SCALE / 800e[scale]` → attacker receives ~91.875 WETH (vs ~87.5 WETH at fair price).
6. Attacker repays flash loan, nets ~4.375 WETH profit (~$4,375 at true price) at the borrower's expense.
7. Borrower's position is partially or fully liquidated despite being healthy at the true market price. [7](#0-6) [4](#0-3)

### Citations

**File:** src/Midnight.sol (L568-568)
```text
        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());
```

**File:** src/Midnight.sol (L581-613)
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
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/Midnight.sol (L649-653)
```text
            if (seizedAssets > 0) {
                repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
            } else {
                seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
            }
```

**File:** src/Midnight.sol (L952-955)
```text
                CollateralParams memory collateralParam = market.collateralParams[i];
                uint256 price = IOracle(collateralParam.oracle).price();
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
```

**File:** SECURITY.md (L26-26)
```markdown
Note: This does not exclude oracle manipulation/flash-loan attacks.
```
