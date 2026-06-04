### Title
Unbounded `rcfThreshold` in `Market` Struct Allows Permanent Disabling of Recovery Close Factor Protection — (File: `src/Midnight.sol`)

---

### Summary

The `Market.rcfThreshold` parameter has no upper-bound validation in `touchMarket`. Because market creation is fully permissionless, any attacker can deploy a market with `rcfThreshold = type(uint256).max`, permanently disabling the Recovery Close Factor (RCF) for every borrower in that market. This allows liquidators to seize an entire position even when only a dust-sized health deficit exists, causing borrowers to lose far more collateral than the protocol's RCF mechanism is designed to permit.

---

### Finding Description

**Root cause — missing bound check in `touchMarket`**

`touchMarket` validates several market parameters at creation time: [1](#0-0) 

- `maturity` is capped at 100 years in the future.
- `lltv` must be one of nine whitelisted values via `isLltvAllowed`.
- `maxLif` must equal one of two deterministic values derived from `lltv`.

`rcfThreshold` receives **no validation whatsoever**. It is stored as part of the immutable market identity (via `IdLib.storeInCode`) and is never checked again after creation. [2](#0-1) 

**How `rcfThreshold` is consumed in `liquidate`**

In normal-mode liquidation the RCF check is: [3](#0-2) 

The condition reads: liquidation is allowed if **either** `repaidUnits <= maxRepaid` (within the RCF cap) **or** the remaining collateral value after max repayment is `< market.rcfThreshold` (RCF bypass for small positions).

When `rcfThreshold = type(uint256).max`, the right-hand side of the `||` is always `true` because every `uint256` value is less than `type(uint256).max`. The RCF cap (`repaidUnits <= maxRepaid`) is therefore **never enforced**, and a liquidator can repay the borrower's entire debt and seize all collateral in a single call, regardless of how small the health deficit is.

**`Market` struct for reference** [4](#0-3) 

`rcfThreshold` is a plain `uint256` with no type-level constraint.

---

### Impact Explanation

The RCF is the primary mechanism that protects borrowers from over-liquidation. Its purpose is to limit a liquidation to only the amount needed to restore the position to health. Disabling it means:

- A borrower whose debt exceeds `maxDebt` by 1 wei can have their **entire collateral** seized.
- The borrower suffers a loss equal to `(fullCollateralValue - minimalRepaymentNeeded)`, which can be orders of magnitude larger than the actual health deficit.
- Because the market is immutable once created, there is no remediation path for borrowers already in the market.

**Impact category**: Direct, permanent financial loss to borrowers — theft of collateral beyond what the protocol's liquidation model permits.

---

### Likelihood Explanation

**Attacker preconditions (no privilege required):**

1. Call `touchMarket` (or any function that triggers it, e.g. `take`) with a `Market` struct where `rcfThreshold = type(uint256).max`. This costs only gas.
2. Act as a lender/maker in the market to attract borrowers (e.g., post competitive buy offers).
3. Monitor borrower health and liquidate fully the moment any position becomes unhealthy.

Market creation is fully permissionless. The attacker needs no admin keys, no governance approval, and no special role. The only social requirement is attracting borrowers, which is realistic given that many users do not inspect raw market parameters before entering a position.

---

### Recommendation

Add an upper-bound check on `rcfThreshold` inside `touchMarket`, analogous to the existing checks on `lltv` and `maxLif`. A sensible upper bound should be derived from the protocol's intended liquidation economics (e.g., a multiple of the maximum loan token supply or a protocol-defined constant). At minimum, reject `rcfThreshold == type(uint256).max`:

```solidity
// In touchMarket, alongside the other per-market validations:
require(market.rcfThreshold <= MAX_RCF_THRESHOLD, RcfThresholdTooHigh());
```

Define `MAX_RCF_THRESHOLD` in `ConstantsLib.sol` at a value that still allows the RCF bypass for genuinely dust-sized positions while preventing the bypass from being permanently active.

---

### Proof of Concept

**Setup:**
- Attacker deploys a market with `rcfThreshold = type(uint256).max`, a real loan token, and a real collateral oracle.
- Attacker posts a buy offer (lender side) at a competitive tick to attract borrowers.

**Execution:**
1. Victim calls `take` on the attacker's offer, increasing their debt. Position is healthy: `debt = 100`, `maxDebt = 101`.
2. Oracle price drops slightly: `debt = 100`, `maxDebt = 99`. Position is unhealthy by 1 unit.
3. `maxRepaid` (RCF cap) = `(100 - 99) * WAD^2 / (WAD^2 - lif*lltv)` ≈ a few units.
4. Attacker calls `liquidate` with `repaidUnits = 100` (full debt).
5. The RCF check evaluates: `100 <= ~2` → **false**; `collateralValue.zeroFloorSub(~2) < type(uint256).max` → **true** (always).
6. `require` passes. Attacker repays 100 units and seizes all collateral at the LIF-discounted price.
7. Victim's entire position is wiped out despite only a 1-unit health deficit. [5](#0-4) [6](#0-5)

### Citations

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

**File:** src/Midnight.sol (L755-773)
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
```

**File:** src/Midnight.sol (L786-788)
```text
            IdLib.storeInCode(market, INITIAL_CHAIN_ID);

            emit EventsLib.MarketCreated(market, id);
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
