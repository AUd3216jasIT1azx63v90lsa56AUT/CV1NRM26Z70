Audit Report

## Title
Missing `loanToken` validation in `touchMarket` allows permanent registration of a broken market where `take()`, `repay()`, and `withdraw()` always revert - (File: src/Midnight.sol)

## Summary
`touchMarket` validates collateral params, maturity, LLTV, and `maxLif` but performs no check on `market.loanToken`, allowing any unprivileged caller to permanently register a market with `loanToken == address(0)`. Once `marketState[id].tickSpacing` is set to `DEFAULT_TICK_SPACING`, the market cannot be unregistered. Every subsequent call to `take()`, `repay()`, and `withdraw()` unconditionally reaches `SafeTransferLib.safeTransferFrom(address(0), ...)` or `safeTransfer(address(0), ...)`, which reverts at the `require(token.code.length > 0, NoCode())` guard, permanently blocking all activity on that market ID.

## Finding Description
**Root cause — missing check in `touchMarket`:**

`touchMarket` (lines 755–791) validates maturity (line 758), collateral params length (lines 759–760), collateral token ordering via `collateralToken > previousCollateralToken` (line 764) — which incidentally blocks `address(0)` as a *collateral* token since `previousCollateralToken` starts as `address(0)`, but says nothing about `loanToken` — per-collateral `lltv` (line 766), and `maxLif` (lines 767–771). There is no `require(market.loanToken != address(0))` or equivalent. [1](#0-0) 

The market state is written unconditionally at line 776:

```solidity
_marketState.tickSpacing = DEFAULT_TICK_SPACING;
```

Once `tickSpacing > 0` is set for a given market ID, the `if (marketState[id].tickSpacing == 0)` guard (line 757) is never entered again. No deletion or reset mechanism exists. [2](#0-1) 

**Exploit flow:**

1. Attacker constructs a `Market` struct with `loanToken = address(0)` and otherwise valid collateral params (sorted, valid LLTV/maxLif, valid maturity).
2. Attacker calls `midnight.touchMarket(market)` — all checks pass, `marketState[id].tickSpacing` is set to `DEFAULT_TICK_SPACING`, market is permanently registered.
3. Any caller invokes `take()` on this market. `take()` calls `touchMarket` again (line 347), which is a no-op since `tickSpacing > 0`. Execution proceeds through all validation and accounting logic, then reaches:

```solidity
// Midnight.sol:455–456
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
``` [3](#0-2) 

4. `SafeTransferLib.safeTransferFrom(address(0), ...)` immediately reverts at:

```solidity
// src/libraries/SafeTransferLib.sol:25
require(token.code.length > 0, NoCode());
``` [4](#0-3) 

`address(0).code.length == 0` always, so this revert is unconditional regardless of `value`. The same applies to `repay()` (line 520: `safeTransferFrom(market.loanToken, ...)`) and `withdraw()` (line 499: `safeTransfer(market.loanToken, ...)`). [5](#0-4) [6](#0-5) 

**Why existing protections are insufficient:**

The `SafeTransferLib.NoCode` guard correctly prevents the ERC-20 call from succeeding, but it fires *after* the market has already been permanently created. The market ID is a deterministic hash of the market parameters; once `tickSpacing > 0` is set for that ID, no mechanism exists to delete or reset it. [7](#0-6) 

## Impact Explanation
The market with `loanToken == address(0)` is permanently registered with `tickSpacing > 0`. Every invocation of `take()`, `repay()`, and `withdraw()` on this market reverts with `NoCode()`, permanently blocking all trading and credit/debt settlement. `supplyCollateral` and `withdrawCollateral` (when no debt exists) still function since they use the collateral token directly, but since `take()` always reverts, no debt can ever be created, making the market entirely non-functional. The DoS is permanent and irrecoverable for that market ID, constituting unrecoverable corruption of protocol state as defined in RESEARCHER.md ("Permanent lock, freeze, or unrecoverable corruption of user/project state"). [8](#0-7) 

## Likelihood Explanation
The precondition is trivially satisfiable: `touchMarket` is a public function with no access control. Constructing a `Market` struct with `loanToken = address(0)` and valid collateral params (sorted, valid LLTV/maxLif, valid maturity) requires no special privilege. The attack is a single transaction. It is repeatable for any market ID that has not yet been created. [9](#0-8) 

## Recommendation
Add an explicit non-zero check for `market.loanToken` at the top of the `if (marketState[id].tickSpacing == 0)` block in `touchMarket`, before any state is written:

```solidity
require(market.loanToken != address(0), InvalidLoanToken());
```

This mirrors the implicit protection already afforded to collateral tokens via the `collateralToken > previousCollateralToken` ordering check (which starts from `address(0)`), and should be placed alongside the other input validations at lines 758–771. [10](#0-9) 

## Proof of Concept
Minimal Foundry test:

```solidity
function test_touchMarket_zeroLoanToken_permanentDoS() public {
    // 1. Build a Market with loanToken = address(0) and one valid collateral
    CollateralParams[] memory params = new CollateralParams[](1);
    params[0] = CollateralParams({
        token: address(0x1),          // non-zero, satisfies ordering check
        lltv: ALLOWED_LLTV,
        maxLif: maxLif(ALLOWED_LLTV, LIQUIDATION_CURSOR_LOW),
        oracle: address(oracle)
    });
    Market memory market = Market({
        loanToken: address(0),        // zero — no check in touchMarket
        collateralParams: params,
        maturity: block.timestamp + 365 days,
        enterGate: address(0)
    });

    // 2. Register the market — succeeds, tickSpacing is now set
    bytes32 id = midnight.touchMarket(market);
    assertGt(midnight.marketState(id).tickSpacing, 0);

    // 3. Any take() on this market reverts with NoCode()
    // (construct a minimal valid offer and call take — omitted for brevity)
    // Expected: revert SafeTransferLib.NoCode()
}
```

### Citations

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/Midnight.sol (L499-499)
```text
        SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
```

**File:** src/Midnight.sol (L520-520)
```text
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
```

**File:** src/Midnight.sol (L755-776)
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

**File:** RESEARCHER.md (L14-14)
```markdown
- Permanent lock, freeze, or unrecoverable corruption of user/project state.
```
