### Title
Missing Zero Address Validation for `loanToken` in `touchMarket` Allows Permanent Market Bricking — (File: src/Midnight.sol)

---

### Summary

The `touchMarket` function in `Midnight.sol` creates immutable markets without validating that `market.loanToken` is a non-zero address. Because markets are permanently immutable once created, a market initialized with `loanToken = address(0)` is irreversibly broken: every subsequent operation that attempts a loan-token transfer will revert, making the market permanently unusable.

---

### Finding Description

`touchMarket` is the market initialization entry point (the protocol's analog to a constructor). It validates collateral token addresses via the sorted-ascending check (`collateralToken > previousCollateralToken`, where `previousCollateralToken` starts at `address(0)`), which implicitly prevents zero-address collateral tokens. However, **no equivalent check exists for `market.loanToken`**. [1](#0-0) 

The relevant gap: `touchMarket` validates collateral token ordering but never asserts `market.loanToken != address(0)`: [2](#0-1) 

`SafeTransferLib.safeTransfer` and `safeTransferFrom` both begin with:

```solidity
require(token.code.length > 0, NoCode());
``` [3](#0-2) [4](#0-3) 

`address(0)` has no code, so every call that transfers the loan token — `take`, `withdraw`, `repay`, `liquidate`, `claimSettlementFee`, `claimContinuousFee` — will revert with `NoCode()` for any market created with `loanToken = address(0)`.

Because markets are immutable once created (`tickSpacing > 0` is the creation sentinel and can never be reset), there is no recovery path.

---

### Impact Explanation

A market created with `loanToken = address(0)` is **permanently and irrecoverably broken**. All loan-token transfer operations revert. No lender can deposit, no borrower can borrow, no fee can be claimed, and no position can be settled. The market state is permanently corrupted with no upgrade or migration path available. [5](#0-4) 

---

### Likelihood Explanation

`touchMarket` is permissionless — any external caller can invoke it directly or indirectly (via `take`, `repay`, `supplyCollateral`, etc.). A market creator who accidentally passes `address(0)` as `loanToken` (e.g., from an uninitialized variable or a misconfigured deployment script) will permanently brick that market with no recourse. A malicious actor can also deliberately create such a market to pollute the market namespace or confuse integrators. [6](#0-5) 

---

### Recommendation

Add an explicit non-zero address check for `market.loanToken` inside `touchMarket`, alongside the existing collateral parameter validations:

```solidity
require(market.loanToken != address(0), ZeroLoanToken());
```

This mirrors the implicit protection already afforded to collateral tokens via the sorted-ascending check. [7](#0-6) 

---

### Proof of Concept

1. Deploy `Midnight`.
2. Construct a `Market` struct with `loanToken = address(0)`, a valid collateral array, and a valid maturity.
3. Call `touchMarket(market)` — succeeds; market is created with `tickSpacing = DEFAULT_TICK_SPACING`.
4. Attempt any loan-token operation (e.g., `withdraw`, `repay`, or `take`) on this market.
5. Every call reverts with `NoCode()` from `SafeTransferLib`.
6. The market is permanently registered in `marketState` with no way to delete or replace it. [8](#0-7) [3](#0-2)

### Citations

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

**File:** src/libraries/SafeTransferLib.sol (L12-13)
```text
    function safeTransfer(address token, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());
```

**File:** src/libraries/SafeTransferLib.sol (L24-25)
```text
    function safeTransferFrom(address token, address from, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());
```
