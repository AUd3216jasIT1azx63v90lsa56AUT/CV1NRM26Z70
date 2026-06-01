### Title
Unvalidated `liquidatorGate` Non-Contract Address Permanently Blocks All Liquidations - (File: src/Midnight.sol)

### Summary

`touchMarket` validates collateral params, maturity, lltv, and maxLif, but performs **no check** that `market.liquidatorGate` is either `address(0)` or a deployed contract. Any unprivileged caller can create a market with `liquidatorGate = address(0xdead)`. Once created, every call to `liquidate()` unconditionally reverts because Solidity's ABI decoder fails to decode a `bool` return value from a CALL to a codeless address, permanently bricking liquidations for that market.

### Finding Description

**Root cause — missing extcodesize check in `touchMarket`:**

`touchMarket` validates only maturity, collateral count, token ordering, lltv, and maxLif. There is no check that `liquidatorGate` is `address(0)` or a contract: [1](#0-0) 

The `Market` struct accepts any `address` for `liquidatorGate`: [2](#0-1) 

**Trigger — `liquidate()` calls the interface on the codeless address:** [3](#0-2) 

When `liquidatorGate = address(0xdead)`:
1. `market.liquidatorGate == address(0)` → `false` (short-circuit does not fire).
2. `ILiquidatorGate(address(0xdead)).canLiquidate(msg.sender)` is evaluated.
3. The EVM `CALL` to a codeless address returns `(success=1, returndata=<empty>)`.
4. Solidity's ABI decoder expects ≥ 32 bytes for the `bool` return; it gets 0 bytes → **unconditional revert** (ABI decoding error), not `LiquidatorGatedFromLiquidating`.
5. The entire `liquidate()` call reverts for every caller, every time.

The `ILiquidatorGate` interface confirms the return type is `bool`: [4](#0-3) 

**Exploit flow:**
1. Attacker calls `touchMarket(market)` with `market.liquidatorGate = address(0xdead)` and otherwise valid params. Market is created successfully.
2. Lenders and borrowers interact normally (enterGate is separate; `take` is unaffected).
3. Borrower's position becomes unhealthy (oracle price drop).
4. Any call to `liquidate(market, ...)` hits the `canLiquidate` call → ABI decode revert → liquidation permanently impossible.
5. Bad debt accumulates; lenders cannot recover funds.

**Existing protections are insufficient:** The `require` guard at line 597–600 does not catch a revert thrown inside the condition expression itself — it only catches a `false` return. A revert inside the `||` operand propagates up unconditionally.

### Impact Explanation

Unhealthy borrowers in any market created with a non-contract `liquidatorGate` can never be liquidated. Bad debt accumulates without bound. Lenders suffer permanent, unrecoverable fund loss. The core invariant — "unhealthy positions remain liquidatable" — is violated permanently for the affected market.

### Likelihood Explanation

- **Precondition:** Attacker creates a market with `liquidatorGate = address(0xdead)` (or any non-contract, non-zero address). This requires no privilege — `touchMarket` is public and permissionless.
- **Feasibility:** Trivial. One transaction suffices to create the malformed market.
- **Repeatability:** The market ID is deterministic from its parameters; once created, the `liquidatorGate` is immutable (encoded in the market struct). The DoS is permanent.
- **Attracting victims:** The attacker can seed the market with attractive offers to draw in lenders and borrowers before the position becomes unhealthy.

### Recommendation

In `touchMarket`, add an extcodesize check for `liquidatorGate` (and symmetrically for `enterGate`):

```solidity
require(
    market.liquidatorGate == address(0) || market.liquidatorGate.code.length > 0,
    InvalidLiquidatorGate()
);
```

This mirrors the pattern already used implicitly for oracle addresses and ensures that any non-zero gate address is a deployed contract capable of returning a valid `bool`.

### Proof of Concept

```solidity
function testDeadLiquidatorGatePermanentlyBlocksLiquidation() public {
    // 1. Build a market with liquidatorGate = address(0xdead)
    Market memory m;
    m.loanToken = address(loanToken);
    m.maturity = block.timestamp + 100;
    m.liquidatorGate = address(0xdead); // non-contract, non-zero
    m.collateralParams.push(CollateralParams({
        token: address(collateralToken1),
        lltv: 0.77e18,
        maxLif: maxLif(0.77e18, LIQUIDATION_CURSOR_LOW),
        oracle: address(oracle1)
    }));

    // 2. Market creation succeeds — no validation of liquidatorGate
    midnight.touchMarket(m);

    // 3. Establish a borrower position (via storage manipulation or normal take flow)
    bytes32 id = toId(m);
    // ... supply collateral, take to create debt ...

    // 4. Make position unhealthy
    oracle1.setPrice(ORACLE_PRICE_SCALE / 2);

    // 5. Assert liquidate always reverts (ABI decode error, not LiquidatorGatedFromLiquidating)
    vm.expectRevert(); // ABI decoding revert, not a named error
    midnight.liquidate(m, 0, 0, 0, borrower, false, address(this), address(0), "");

    // 6. Confirm borrower still has debt — bad debt accumulates
    assertGt(midnight.debtOf(id, borrower), 0);
}
```

**Expected assertions:**
- `touchMarket` with `liquidatorGate = address(0xdead)` does **not** revert (missing check).
- `liquidate` reverts with an ABI decoding error (not `LiquidatorGatedFromLiquidating`).
- `debtOf(id, borrower) > 0` after the failed liquidation attempt.

### Citations

**File:** src/Midnight.sol (L597-600)
```text
        require(
            market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
            LiquidatorGatedFromLiquidating()
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

**File:** src/interfaces/IGate.sol (L10-12)
```text
interface ILiquidatorGate {
    function canLiquidate(address account) external view returns (bool);
}
```
