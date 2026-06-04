### Title
Unbounded `rcfThreshold` in `Market` struct disables Recovery Close Factor protection, enabling over-liquidation of borrowers — (`src/Midnight.sol`)

---

### Summary

The `rcfThreshold` field of the `Market` struct is never validated in `touchMarket()`. Since market creation is permissionless, an attacker can deploy a market with `rcfThreshold = type(uint256).max`. This permanently disables the Recovery Close Factor (RCF) guard in `liquidate()`, allowing liquidators to seize an entire position even when only a partial liquidation is needed to restore health, causing direct collateral loss to borrowers.

---

### Finding Description

**Vulnerability class:** Missing input validation / unbounded parameter controlling a risk limit (direct analog to the external report's unbounded `oracleSlippagePercentOrLimit`).

**Root cause — no bound on `rcfThreshold` in `touchMarket()`:**

`touchMarket()` validates `maturity`, `lltv`, and `maxLif`, but never checks `rcfThreshold`: [1](#0-0) 

The validated fields are:
- `market.maturity` — bounded to 100 years [2](#0-1) 
- `lltv` — whitelisted values only [3](#0-2) 
- `maxLif` — constrained to two specific cursor values [4](#0-3) 

`rcfThreshold` is part of the `Market` struct but receives **no validation**: [5](#0-4) 

**How `rcfThreshold` is consumed in `liquidate()`:** [6](#0-5) 

The RCF check passes (liquidation proceeds) if **either**:
1. `repaidUnits <= maxRepaid` — within the RCF cap, **or**
2. `(collateral_value - maxRepaid) < market.rcfThreshold` — the residual collateral is below the dust threshold

If `rcfThreshold = type(uint256).max`, condition 2 evaluates to `someUint256 < type(uint256).max`, which is **always true** for any realistic collateral value. The RCF cap is permanently bypassed in normal (pre-maturity) mode.

The protocol's own documentation confirms the intended semantics:

> *"The RCF is deactivated for small collateral amount, essentially to mitigate issues with liquidations that are too small compared to the gas cost."* [7](#0-6) 

Setting `rcfThreshold = type(uint256).max` violates this intent entirely.

---

### Impact Explanation

A borrower who is only slightly unhealthy (e.g., debt = 101, maxDebt = 100) should, under the RCF, have only a small portion of their position liquidated — just enough to restore health. With `rcfThreshold = type(uint256).max`, a liquidator can repay the entire debt and seize all corresponding collateral in a single call. The borrower suffers a **direct, unnecessary collateral loss** proportional to the excess liquidation beyond what the RCF would have permitted.

This maps directly to the external report's impact: *"Trade might be settled with a large slippage causing a loss of assets"* — here, a liquidation seizes far more collateral than the protocol's risk model intends.

---

### Likelihood Explanation

Market creation is **permissionless** — `touchMarket()` is callable by anyone. The `live_context.json` explicitly lists `"market creator"` as a valid attacker model: [8](#0-7) 

An attacker creates a market with `rcfThreshold = type(uint256).max` and all other parameters appearing legitimate (valid `lltv`, valid `maxLif`, reasonable maturity). Borrowers who do not inspect `rcfThreshold` — a non-obvious internal parameter — enter the market and become vulnerable to full liquidation on any health breach.

---

### Recommendation

Add an upper bound on `rcfThreshold` inside `touchMarket()`, analogous to how `maxSettlementFee` bounds settlement fees and `isLltvAllowed` bounds LLTV:

```solidity
// In touchMarket(), alongside the other collateral param checks:
require(market.rcfThreshold <= MAX_RCF_THRESHOLD, RcfThresholdTooHigh());
```

`MAX_RCF_THRESHOLD` should be set to a value that represents a realistic dust threshold (e.g., a few hundred loan token units), preventing the RCF from being silently disabled.

---

### Proof of Concept

1. Attacker calls `touchMarket()` with a `Market` where `rcfThreshold = type(uint256).max` and all other fields are valid.
2. `touchMarket()` succeeds — no check on `rcfThreshold`.
3. Victim borrower supplies collateral and takes a sell offer (increases debt) in this market.
4. Oracle price drops slightly; borrower becomes unhealthy by 1 unit (`debt = maxDebt + 1`).
5. Attacker calls `liquidate()` with `repaidUnits = position.debt` (full liquidation), `postMaturityMode = false`.
6. The RCF check at line 662–667 evaluates:
   - `repaidUnits > maxRepaid` (full repayment exceeds the RCF cap) → condition 1 fails
   - `collateral_value.zeroFloorSub(maxRepaid) < type(uint256).max` → **always true** → condition 2 passes
7. Full liquidation executes. Borrower loses all collateral instead of the small fraction the RCF would have permitted. [6](#0-5) [1](#0-0)

### Citations

**File:** src/Midnight.sol (L70-75)
```text
/// @dev The RCF is deactivated for small collateral amount, essentially to mitigate issues with liquidations that are
/// too small compared to the gas cost. More precisely, it is deactivated if the liquidation could leave a collateral
/// with a value that would not be enough to repay rcfThreshold units. Which means (omitting scaling and roundings):
///   minNewCollateral * liquidatedCollatPrice / LIF < rcfThreshold
///     <=> (collateral - maxRepaid * LIF / liquidatedCollatPrice) * liquidatedCollatPrice / LIF < rcfThreshold
///     <=> collateral * liquidatedCollatPrice / LIF - maxRepaid < rcfThreshold
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

**File:** src/Midnight.sol (L755-791)
```text
    function touchMarket(Market memory market) public returns (bytes32) {
        bytes32 id = toId(market);
        if (marketState[id].tickSpacing == 0) {
            require(market.maturity <= block.timestamp + 100 * 365 days, MaturityTooFar());
            require(market.collateralParams.length > 0, NoCollateralParams());
            require(market.collateralParams.length <= MAX_COLLATERALS, TooManyCollateralParams());
            address previousCollateralToken;
            for (uint256 i = 0; i < market.collateralParams.length; i++) {
                address collateralToken = market.collateralParams[i].token;
                require(collateralToken > previousCollateralToken, CollateralParamsNotSorted());
                uint256 lltv = market.collateralParams[i].lltv;
                require(isLltvAllowed(lltv), LltvNotAllowed());
                require(
                    market.collateralParams[i].maxLif == maxLif(lltv, LIQUIDATION_CURSOR_LOW)
                        || market.collateralParams[i].maxLif == maxLif(lltv, LIQUIDATION_CURSOR_HIGH),
                    InvalidMaxLif()
                );
                previousCollateralToken = collateralToken;
            }

            MarketState storage _marketState = marketState[id];
            _marketState.tickSpacing = DEFAULT_TICK_SPACING;
            uint16[7] memory _defaultSettlementFeeCbp = defaultSettlementFeeCbp[market.loanToken];
            _marketState.settlementFeeCbp0 = _defaultSettlementFeeCbp[0];
            _marketState.settlementFeeCbp1 = _defaultSettlementFeeCbp[1];
            _marketState.settlementFeeCbp2 = _defaultSettlementFeeCbp[2];
            _marketState.settlementFeeCbp3 = _defaultSettlementFeeCbp[3];
            _marketState.settlementFeeCbp4 = _defaultSettlementFeeCbp[4];
            _marketState.settlementFeeCbp5 = _defaultSettlementFeeCbp[5];
            _marketState.settlementFeeCbp6 = _defaultSettlementFeeCbp[6];
            _marketState.continuousFee = defaultContinuousFee[market.loanToken];
            IdLib.storeInCode(market, INITIAL_CHAIN_ID);

            emit EventsLib.MarketCreated(market, id);
        }
        return id;
    }
```

**File:** src/interfaces/IMidnight.sol (L5-12)
```text
struct Market {
    address loanToken;
    CollateralParams[] collateralParams;
    uint256 maturity;
    uint256 rcfThreshold;
    address enterGate;
    address liquidatorGate;
}
```

**File:** live_context.json (L29-42)
```json
  "audit_mode": {
    "attacker_model": [
      "unprivileged borrower",
      "unprivileged lender",
      "maker",
      "taker",
      "liquidator",
      "market creator",
      "callback receiver",
      "authorized operator",
      "signature user",
      "router/composer using multicall"
    ],
    "do_not_rely_on": [
```
