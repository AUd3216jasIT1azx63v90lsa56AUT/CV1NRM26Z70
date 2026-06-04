### Title
Implicit Market Creation via `CREATE2` in `touchMarket` Causes Unexpected Gas Costs for Users - (File: src/Midnight.sol)

---

### Summary

Every user-facing entry point (`take`, `withdraw`, `repay`, `supplyCollateral`, `withdrawCollateral`, `liquidate`) calls `touchMarket`, which silently deploys a new SStore2 contract via `CREATE2` when the market does not yet exist. Any user who is first to interact with a valid-but-uncreated market pays for this deployment without explicit consent, mirroring the M-08 pattern exactly.

---

### Finding Description

`touchMarket` checks `marketState[id].tickSpacing == 0` to detect a new market. When true, it validates parameters and then calls `IdLib.storeInCode`, which executes a raw `CREATE2` opcode to deploy a contract whose runtime bytecode is the ABI-encoded `Market` struct (the SStore2 pattern). [1](#0-0) [2](#0-1) 

The gas cost of the `CREATE2` deployment scales directly with the size of the `Market` struct. Each `CollateralParams` entry ABI-encodes to ~160 bytes. With `MAX_COLLATERALS = 128`:

- Bytecode size: `11 (SSTORE2_PREFIX) + 128 × 160 ≈ 20,491 bytes`
- Deployment gas: `32,000 (CREATE2 base) + 20,491 × 200 ≈ 4,130,200 gas` [3](#0-2) 

Even a minimal market (1 collateral param, ~300 bytes) adds ~60,000 gas to the first caller's transaction.

All six entry points call `touchMarket` unconditionally before any user-specific logic: [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) 

---

### Impact Explanation

The first user to call any entry point on a valid-but-uncreated market silently pays for the `CREATE2` deployment. For a maximum-collateral market (128 params), this is ~4 million gas — hundreds of dollars at typical gas prices — charged to the user with no warning and no separate confirmation step. Funds are not stolen, but the user bears a large unexpected cost they did not consent to. [10](#0-9) 

---

### Likelihood Explanation

This is triggered by normal protocol usage: any taker executing the first `take` on a new market, or any user calling `supplyCollateral` on a freshly parameterized market, will hit this path. Markets are permissionlessly created by anyone passing valid parameters, so the scenario is routine, not edge-case. [11](#0-10) 

---

### Recommendation

Separate market creation from market interaction. Add a dedicated `createMarket(Market memory market)` function and revert in all entry points if `marketState[id].tickSpacing == 0` (`MarketNotCreated`). This makes the expensive `CREATE2` deployment an explicit, user-initiated action — exactly the mitigation recommended in M-08.

---

### Proof of Concept

1. Deploy `Midnight`.
2. Construct a valid `Market` struct with 128 `CollateralParams` entries (all valid LLTV tiers, valid `maxLif` values).
3. Have a maker sign a buy offer for this market.
4. Call `take(offer, ratifierData, units, taker, ...)` — the market does not yet exist.
5. `touchMarket` fires `IdLib.storeInCode` via `CREATE2`, deploying ~20 KB of bytecode.
6. The taker's transaction succeeds but consumed ~4 million extra gas for market creation they never explicitly requested. [1](#0-0) [2](#0-1)

### Citations

**File:** src/Midnight.sol (L347-347)
```text
        bytes32 id = touchMarket(offer.market);
```

**File:** src/Midnight.sol (L483-483)
```text
        bytes32 id = touchMarket(market);
```

**File:** src/Midnight.sol (L506-506)
```text
        bytes32 id = touchMarket(market);
```

**File:** src/Midnight.sol (L528-528)
```text
        bytes32 id = touchMarket(market);
```

**File:** src/Midnight.sol (L557-557)
```text
        bytes32 id = touchMarket(market);
```

**File:** src/Midnight.sol (L592-592)
```text
        bytes32 id = touchMarket(market);
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

**File:** src/libraries/IdLib.sol (L35-41)
```text
    function storeInCode(Market memory market, uint256 chainId) internal returns (address create2Address) {
        bytes memory creationCode = abi.encodePacked(SSTORE2_PREFIX, abi.encode(market));
        assembly ("memory-safe") {
            create2Address := create2(0, add(creationCode, 0x20), mload(creationCode), chainId)
        }
        require(create2Address != address(0), SStore2DeploymentFailed());
    }
```

**File:** src/libraries/ConstantsLib.sol (L20-20)
```text
uint256 constant MAX_COLLATERALS = 128;
```
