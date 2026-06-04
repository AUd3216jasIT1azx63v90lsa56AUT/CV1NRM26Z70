### Title
Missing Oracle Address Validation in `touchMarket()` Enables Permanent Collateral Lock — (File: `src/Midnight.sol`)

---

### Summary

The `touchMarket()` function validates several market parameters (maturity upper bound, collateral list length, LLTV tiers, `maxLif` values) but does not validate that `market.collateralParams[i].oracle` is non-zero. A malicious maker can craft an offer embedding a market with `oracle = address(0)`, lure a taker into taking it as a seller (borrower), and permanently lock the borrower's collateral because every downstream oracle call reverts. Additionally, `market.loanToken` is never validated as non-zero, which renders any such market entirely non-functional.

---

### Finding Description

**Root cause — `touchMarket()` does not validate `oracle != address(0)`:** [1](#0-0) 

The loop validates `token`, `lltv`, and `maxLif` for each `CollateralParams` entry, but `market.collateralParams[i].oracle` is never checked. A market with `oracle = address(0)` is accepted and permanently stored via `IdLib.storeInCode`. [2](#0-1) 

**Downstream impact path 1 — `isHealthy()` reverts:** [3](#0-2) 

`IOracle(address(0)).price()` is a high-level Solidity call to an address with no code. The EVM returns empty data; Solidity's ABI decoder expects a `uint256` return value and reverts unconditionally whenever the borrower has non-zero debt.

**Downstream impact path 2 — `withdrawCollateral()` reverts:** [4](#0-3) 

`withdrawCollateral()` calls `isHealthy()` before transferring tokens. With a zero oracle, any borrower with debt can never pass this check, making their collateral permanently unrecoverable.

**Downstream impact path 3 — `liquidate()` reverts:** [5](#0-4) 

`liquidate()` iterates over all activated collaterals and calls `IOracle(_collateralParam.oracle).price()` unconditionally. With `oracle = address(0)`, this reverts before any liquidation logic executes, making the position permanently un-liquidatable.

**Secondary issue — `loanToken` not validated as non-zero:** [6](#0-5) 

`SafeTransferLib` checks `token.code.length > 0` and reverts with `NoCode()` for `address(0)`, making any market with `loanToken = address(0)` non-functional for all token operations. [7](#0-6) 

---

### Impact Explanation

A borrower who takes a sell offer in a market with `oracle = address(0)` and subsequently supplies collateral has their collateral permanently frozen:

- `withdrawCollateral()` always reverts (oracle call fails inside `isHealthy()`).
- `liquidate()` always reverts (oracle call fails in the collateral loop).
- There is no escape hatch or admin override in the singleton contract.

The collateral is irrecoverably locked for the lifetime of the contract. This constitutes a **permanent loss of funds** for the victim.

---

### Likelihood Explanation

The attack requires no privileged access. Any unprivileged actor can:

1. Construct a `Market` struct with `collateralParams[0].oracle = address(0)`.
2. Sign an offer embedding that market (via `EcrecoverRatifier` or any ratifier they control).
3. Advertise the offer through any off-chain channel.

Victims are realistic: the `Market` struct is complex, frontends typically do not surface raw oracle addresses, and the market creation validation gives a false sense of correctness (it validates LLTV, `maxLif`, token ordering — but silently accepts a zero oracle). A user who sees a well-priced offer has no on-chain signal that the oracle is invalid until after they have supplied collateral and attempted to withdraw.

---

### Recommendation

Add a non-zero oracle check inside the `touchMarket()` collateral validation loop:

```solidity
// In touchMarket(), inside the for-loop (src/Midnight.sol ~line 762)
require(market.collateralParams[i].oracle != address(0), InvalidOracle());
```

Also add a non-zero loan token check:

```solidity
require(market.loanToken != address(0), InvalidLoanToken());
```

Both checks are consistent with the existing pattern of validating all market parameters at creation time, since markets are immutable once created. [8](#0-7) 

---

### Proof of Concept

```
Preconditions:
  - Attacker controls a maker key and a ratifier (e.g. EcrecoverRatifier).
  - Victim is an unprivileged taker.

Steps:
1. Attacker constructs Market M with:
     loanToken  = <valid ERC-20>
     maturity   = block.timestamp + 30 days
     collateralParams[0].token   = <valid collateral ERC-20>
     collateralParams[0].lltv    = LLTV_5 (0.945e18)
     collateralParams[0].maxLif  = maxLif(LLTV_5, LIQUIDATION_CURSOR_LOW)
     collateralParams[0].oracle  = address(0)   ← zero oracle

2. Attacker signs Offer O (buy=false, maker=attacker) embedding M.
   touchMarket(M) passes all existing require checks and creates the market.

3. Victim calls take(O, ...) as taker (seller/borrower).
   Market M is now live with tickSpacing > 0.
   Victim's position.debt > 0.

4. Victim calls supplyCollateral(M, 0, amount, victim).
   Succeeds — supplyCollateral does NOT call the oracle.
   position[id][victim].collateral[0] = amount.

5. Victim calls withdrawCollateral(M, 0, amount, victim, victim).
   → touchMarket(M) returns existing id.
   → isHealthy(M, id, victim) is called.
   → debt > 0, so oracle loop executes.
   → IOracle(address(0)).price() → EVM call to address(0) returns empty data.
   → Solidity ABI-decode of uint256 from empty data → REVERT.
   withdrawCollateral reverts. Collateral is locked.

6. Any liquidator calls liquidate(M, 0, 0, 0, victim, true, ...).
   → liquidate() iterates collateralBitmap.
   → IOracle(address(0)).price() → REVERT.
   Liquidation is permanently blocked.

Result: victim's collateral is irrecoverably locked.
```

### Citations

**File:** src/Midnight.sol (L568-568)
```text
        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());
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

**File:** src/Midnight.sol (L757-773)
```text
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

**File:** src/Midnight.sol (L777-785)
```text
            uint16[7] memory _defaultSettlementFeeCbp = defaultSettlementFeeCbp[market.loanToken];
            _marketState.settlementFeeCbp0 = _defaultSettlementFeeCbp[0];
            _marketState.settlementFeeCbp1 = _defaultSettlementFeeCbp[1];
            _marketState.settlementFeeCbp2 = _defaultSettlementFeeCbp[2];
            _marketState.settlementFeeCbp3 = _defaultSettlementFeeCbp[3];
            _marketState.settlementFeeCbp4 = _defaultSettlementFeeCbp[4];
            _marketState.settlementFeeCbp5 = _defaultSettlementFeeCbp[5];
            _marketState.settlementFeeCbp6 = _defaultSettlementFeeCbp[6];
            _marketState.continuousFee = defaultContinuousFee[market.loanToken];
```

**File:** src/Midnight.sol (L786-789)
```text
            IdLib.storeInCode(market, INITIAL_CHAIN_ID);

            emit EventsLib.MarketCreated(market, id);
        }
```

**File:** src/Midnight.sol (L952-953)
```text
                CollateralParams memory collateralParam = market.collateralParams[i];
                uint256 price = IOracle(collateralParam.oracle).price();
```

**File:** src/libraries/SafeTransferLib.sol (L12-13)
```text
    function safeTransfer(address token, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());
```
