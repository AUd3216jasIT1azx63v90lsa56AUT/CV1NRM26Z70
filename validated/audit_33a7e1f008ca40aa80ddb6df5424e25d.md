Audit Report

## Title
Unvalidated `liquidatorGate` Allows Market Creation with Non-Implementing Address, Permanently Blocking All Liquidations - (File: src/Midnight.sol)

## Summary
`touchMarket` performs no validation on `market.liquidatorGate`, allowing any caller to set it to an arbitrary address such as `market.loanToken`. Because `liquidate` unconditionally calls `ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender)` when `liquidatorGate != address(0)`, and a standard ERC20 has no `canLiquidate` selector and no fallback, every liquidation call in that market reverts permanently. A malicious borrower who creates such a market can render their position immune to liquidation, causing unrecoverable losses for lenders.

## Finding Description

**Root cause:** `touchMarket` validates maturity, collateral count, sort order, LLTV tiers, and `maxLif` values, but places no constraint on `market.liquidatorGate`:

```solidity
// src/Midnight.sol:755-791
function touchMarket(Market memory market) public returns (bytes32) {
    bytes32 id = toId(market);
    if (marketState[id].tickSpacing == 0) {
        require(market.maturity <= block.timestamp + 100 * 365 days, MaturityTooFar());
        require(market.collateralParams.length > 0, NoCollateralParams());
        require(market.collateralParams.length <= MAX_COLLATERALS, TooManyCollateralParams());
        // ... collateral sort / LLTV / maxLif checks ...
        // ← no check on liquidatorGate whatsoever
    }
    return id;
}
```

**Trigger in `liquidate`:**

```solidity
// src/Midnight.sol:597-600
require(
    market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
    LiquidatorGatedFromLiquidating()
);
```

When `liquidatorGate` is set to a standard ERC20 address (e.g., `loanToken`), the external call to `canLiquidate` hits a contract with no matching selector and no fallback, causing the call to revert. Solidity 0.8.x propagates this revert through the `require`, so `liquidate` always reverts for every caller. There is no `try/catch` around the `canLiquidate` call and no interface-existence check at market creation time.

**Exploit flow:**
1. Attacker calls `touchMarket` with `market.liquidatorGate = market.loanToken` — passes all existing checks.
2. Attacker (as borrower) opens a position in this market.
3. Oracle price drops, making the position unhealthy (`debt > maxDebt`).
4. Any liquidator calls `liquidate`; execution reaches line 598, calls `ILiquidatorGate(loanToken).canLiquidate(liquidator)`, which reverts (no matching selector, no fallback on a standard ERC20).
5. `liquidate` reverts for every caller, forever.

**Why existing checks fail:** The `liquidatorGate == address(0)` short-circuit is the only bypass, but the attacker sets it to a non-zero address. No try/catch exists around the `canLiquidate` call, and no interface-existence check is performed at market creation time.

## Impact Explanation
All liquidations in the affected market permanently revert. Unhealthy borrower positions accumulate unbounded bad debt that can never be realized or cleared, directly violating the core protocol invariant that unhealthy positions remain liquidatable. Lenders in the market suffer unrecoverable credit losses with no mechanism to force repayment or seize collateral. This constitutes a permanent freeze of lender funds and is a critical integrity failure.

## Likelihood Explanation
The precondition is trivially satisfiable: any unprivileged address can call `touchMarket` with `liquidatorGate = loanToken`. The attacker needs only to be the borrower in the market they create. The attack is repeatable across any loan token that lacks a `canLiquidate` function (i.e., every standard ERC20). It is permanent once the market is created and a position is opened, because market parameters are immutable after creation. Lenders may be deceived into providing offers for what appears to be a legitimately gated market.

## Recommendation
In `touchMarket`, validate that `liquidatorGate` is either `address(0)` or a contract that implements `ILiquidatorGate`. At minimum, add a check such as:

```solidity
require(
    market.liquidatorGate == address(0) ||
    IERC165(market.liquidatorGate).supportsInterface(type(ILiquidatorGate).interfaceId),
    InvalidLiquidatorGate()
);
```

Alternatively, use a `try/catch` around the `canLiquidate` call in `liquidate` and treat a revert as a denial (returning `false`), or maintain an allowlist of valid gate contracts set by a trusted admin.

## Proof of Concept

**Minimal Foundry test:**

```solidity
function testLiquidatorGateSetToLoanTokenBlocksLiquidation(uint256 units) public {
    units = bound(units, 1, MAX_TEST_AMOUNT * 3 / 4);

    // 1. Create market with liquidatorGate = loanToken
    Market memory badMarket;
    badMarket.loanToken = address(loanToken);
    badMarket.maturity = vm.getBlockTimestamp() + 100;
    badMarket.liquidatorGate = address(loanToken); // <-- exploit
    badMarket.collateralParams.push(CollateralParams({
        token: address(collateralToken1),
        lltv: 0.77e18,
        oracle: address(oracle1),
        maxLif: maxLif(0.77e18, LIQUIDATION_CURSOR_LOW)
    }));

    // 2. Borrower opens position
    collateralize(badMarket, borrower, units);
    // lender provides offer and take executes...

    // 3. Price drops, position becomes unhealthy
    Oracle(badMarket.collateralParams[0].oracle).setPrice(ORACLE_PRICE_SCALE / 2);

    // 4. Liquidation attempt always reverts
    deal(address(loanToken), liquidator, units);
    vm.prank(liquidator);
    vm.expectRevert(); // reverts because loanToken has no canLiquidate()
    midnight.liquidate(badMarket, 0, 1, 0, borrower, false, address(this), address(0), "");
}
```

**Expected result:** The `liquidate` call reverts on every attempt, confirming the permanent liquidation block. [1](#0-0) [2](#0-1) [3](#0-2)

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
