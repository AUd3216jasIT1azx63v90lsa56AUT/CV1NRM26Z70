Audit Report

## Title
Missing `loanToken` Validation in `touchMarket` Allows Permanently Unusable Market Creation - (File: `src/Midnight.sol`)

## Summary
`touchMarket` validates collateral tokens, LLTV, `maxLif`, and maturity but performs no check on `market.loanToken`. An unprivileged caller can pass `loanToken = address(0)`, which succeeds and permanently writes `tickSpacing = DEFAULT_TICK_SPACING`. Every subsequent `take` call unconditionally invokes `SafeTransferLib.safeTransferFrom(address(0), ...)`, which reverts with `NoCode()` because `address(0).code.length == 0`, making the market permanently unusable and its ID slot unrecoverable.

## Finding Description

**Root cause — missing check in `touchMarket`:**

`touchMarket` validates `market.maturity` (line 758), `collateralParams.length` (lines 759–760), sorted collateral tokens (line 764), `isLltvAllowed(lltv)` (line 766), and `maxLif` (lines 767–771), but contains no `require(market.loanToken != address(0), ...)` or equivalent guard. [1](#0-0) 

**State change — market permanently marked as created:**

On first call, `_marketState.tickSpacing = DEFAULT_TICK_SPACING` is written (line 776). The re-entry guard `if (marketState[id].tickSpacing == 0)` (line 757) then permanently blocks re-initialization of the same market ID. [2](#0-1) 

**Trigger — `take` calls `safeTransferFrom` unconditionally with `loanToken`:**

After all position accounting, `take` calls `SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets)` and `SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets)` at lines 455–456 with no `if (value > 0)` guard. [3](#0-2) 

**`SafeTransferLib.safeTransferFrom` has an unconditional code-length check:**

The very first line of `safeTransferFrom` is `require(token.code.length > 0, NoCode())` (line 25). Since `address(0).code.length == 0`, this reverts with `NoCode()` on every invocation, regardless of `value`. [4](#0-3) 

**Exploit flow:**
1. Attacker constructs a `Market` with `loanToken = address(0)`, valid sorted collateral tokens, valid LLTV/`maxLif`, and valid maturity.
2. Attacker calls `touchMarket(market)` — succeeds; `tickSpacing > 0`; market ID slot consumed.
3. Any call to `take` with an offer referencing this market reverts at `safeTransferFrom(address(0), ...)` with `NoCode()`.
4. The `if (marketState[id].tickSpacing == 0)` guard prevents any re-creation of this market ID.

## Impact Explanation
The market is permanently created (state written, `tickSpacing > 0`) but every `take`, `withdraw`, `repay`, and `liquidate` that reaches a `safeTransferFrom` or `safeTransfer` on `loanToken` will revert. The market ID for the `(address(0), collaterals, maturity)` tuple is consumed forever. This constitutes a permanent denial-of-service / griefing of specific market configurations at zero cost to the attacker. [5](#0-4) 

## Likelihood Explanation
No preconditions. `touchMarket` is `public` with no access control. Any EOA or contract can call it with `loanToken = address(0)` and any valid collateral/LLTV/`maxLif` combination. The attack costs only gas, is deterministic, and is repeatable for every distinct `(collaterals, maturity)` tuple, allowing an attacker to brick an arbitrary number of market slots. [6](#0-5) 

## Recommendation
Add a zero-address check for `market.loanToken` at the top of the `if (marketState[id].tickSpacing == 0)` block in `touchMarket`, before any state is written:

```solidity
require(market.loanToken != address(0), InvalidLoanToken());
```

This should be placed alongside the existing `market.maturity` check at line 758, before `_marketState.tickSpacing` is written at line 776. [2](#0-1) 

## Proof of Concept
```solidity
// Minimal PoC (Foundry)
function testBrickMarketWithZeroLoanToken() public {
    Market memory market = Market({
        loanToken: address(0),
        collateralParams: validCollateralParams, // valid sorted collateral, lltv, maxLif
        maturity: block.timestamp + 30 days,
        enterGate: address(0)
    });

    // Step 1: touchMarket succeeds with loanToken = address(0)
    bytes32 id = midnight.touchMarket(market);
    assertGt(midnight.marketState(id).tickSpacing, 0); // slot consumed

    // Step 2: touchMarket cannot re-create the same ID
    bytes32 id2 = midnight.touchMarket(market); // no-op, returns same id
    assertEq(id, id2);

    // Step 3: any take on this market reverts with NoCode()
    // (construct a minimal offer referencing this market and call take)
    vm.expectRevert(SafeTransferLib.NoCode.selector);
    midnight.take(offer, 1e18, ...);
}
``` [4](#0-3)

### Citations

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
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

**File:** src/libraries/SafeTransferLib.sol (L24-25)
```text
    function safeTransferFrom(address token, address from, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());
```
