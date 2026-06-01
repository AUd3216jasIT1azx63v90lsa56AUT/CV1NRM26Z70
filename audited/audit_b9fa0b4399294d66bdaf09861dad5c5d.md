### Title
Unvalidated `liquidatorGate` Allows Market Creation with Non-Gate Loan Token Address, Permanently Blocking All Liquidations - (File: src/Midnight.sol)

### Summary
`touchMarket` performs no validation on `market.liquidatorGate`, allowing any caller to set it to `market.loanToken`. Because `liquidate` unconditionally calls `ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender)` when `liquidatorGate != address(0)`, and a standard ERC20 loan token has no `canLiquidate` selector and no fallback, every liquidation call in that market reverts permanently. A borrower who is also the market creator can exploit this to make their own position immune to liquidation.

### Finding Description
**Root cause:** `touchMarket` validates maturity, collateral count, sort order, LLTV tiers, and maxLif values, but places **no constraint on `market.liquidatorGate`**.

```
// src/Midnight.sol:755-791 — full touchMarket validation block
require(market.maturity <= block.timestamp + 100 * 365 days, MaturityTooFar());
require(market.collateralParams.length > 0, NoCollateralParams());
require(market.collateralParams.length <= MAX_COLLATERALS, TooManyCollateralParams());
// ... collateral sort / LLTV / maxLif checks ...
// ← no check on liquidatorGate whatsoever
```

**Trigger in `liquidate`:**

```
// src/Midnight.sol:597-600
require(
    market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
    LiquidatorGatedFromLiquidating()
);
```

When `liquidatorGate` is set to the loan token address (a standard ERC20 with no `canLiquidate(address)` selector and no fallback), the external call reverts. Solidity 0.8.x propagates this revert through the `require`, so `liquidate` always reverts for every caller.

**Exploit flow:**
1. Attacker (who will also be the borrower) calls `touchMarket` with `market.liquidatorGate = market.loanToken` — passes all existing checks.
2. Attacker supplies collateral and takes a borrow offer in this market.
3. Oracle price drops, making the position unhealthy (`debt > maxDebt`).
4. Any liquidator calls `liquidate`; execution reaches line 598, calls `ILiquidatorGate(loanToken).canLiquidate(liquidator)`, which reverts (no matching selector, no fallback on a standard ERC20).
5. `liquidate` reverts for every caller, forever.

**Why existing checks fail:** The `liquidatorGate == address(0)` short-circuit is the only bypass, but the attacker sets it to a non-zero address. There is no try/catch around the `canLiquidate` call, and no interface-existence check at market creation time.

### Impact Explanation
All liquidations in the affected market permanently revert. Unhealthy borrower positions accumulate unbounded bad debt that can never be realized or cleared, directly violating the core invariant: *unhealthy positions remain liquidatable*. Lenders in the market suffer unrecoverable credit losses with no mechanism to force repayment or seize collateral.

### Likelihood Explanation
The precondition is trivially satisfiable: any address can call `touchMarket` with `liquidatorGate = loanToken`. The attacker needs only to be the borrower in the market they create. The attack is repeatable across any loan token that lacks a `canLiquidate` function (i.e., every standard ERC20). It is permanent once the market is created and a position is opened, because market parameters are immutable after creation.

### Recommendation
In `touchMarket`, add a validation that `liquidatorGate` is either `address(0)` or a contract that implements `ILiquidatorGate`. At minimum, reject `liquidatorGate == loanToken` and `liquidatorGate == collateralToken`. A stronger fix is to perform a static call to `canLiquidate` during market creation and require it does not revert:

```solidity
if (market.liquidatorGate != address(0)) {
    // Ensure the gate is a valid ILiquidatorGate implementation
    require(market.liquidatorGate != market.loanToken, InvalidLiquidatorGate());
    // Optionally: probe the interface
    (bool ok,) = market.liquidatorGate.staticcall(
        abi.encodeCall(ILiquidatorGate.canLiquidate, (address(0)))
    );
    require(ok, InvalidLiquidatorGate());
}
```

### Proof of Concept
```solidity
// Foundry unit test
function testLiquidatorGateEqualsLoanTokenBlocksLiquidation() public {
    // 1. Create market with liquidatorGate = loanToken (standard ERC20, no canLiquidate)
    Market memory m;
    m.loanToken      = address(loanToken);          // standard ERC20
    m.liquidatorGate = address(loanToken);          // ← the bug: no check prevents this
    m.maturity       = block.timestamp + 100;
    m.collateralParams.push(CollateralParams({
        token:  address(collateralToken1),
        lltv:   0.77e18,
        maxLif: maxLif(0.77e18, LIQUIDATION_CURSOR_LOW),
        oracle: address(oracle1)
    }));
    midnight.touchMarket(m);

    // 2. Open a borrow position
    collateralize(m, borrower, 1e18);
    take(1e18, lender, makeBorrowerOffer(m));

    // 3. Make position unhealthy
    oracle1.setPrice(ORACLE_PRICE_SCALE / 10); // price crash

    // 4. Assert liquidate always reverts — loanToken has no canLiquidate selector
    vm.expectRevert(); // reverts because loanToken.canLiquidate() reverts
    midnight.liquidate(m, 0, 1, 0, borrower, false, address(this), address(0), "");

    // Invariant: unhealthy position is NOT liquidatable — violated
    assertTrue(midnight.debtOf(toId(m), borrower) > 0, "debt remains");
}
```

Expected assertion: `liquidate` reverts on every call; `debtOf` remains non-zero; the position is permanently stuck. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** src/interfaces/IGate.sol (L10-12)
```text
interface ILiquidatorGate {
    function canLiquidate(address account) external view returns (bool);
}
```
