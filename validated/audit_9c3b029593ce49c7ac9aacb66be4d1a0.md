All code references check out. Let me verify the exact behavior of `supplyCollateral` with respect to state mutation before the revert.

Audit Report

## Title
Missing Bytecode Check in `touchMarket` Allows Permanent Creation of Unusable Markets - (File: src/Midnight.sol)

## Summary
`touchMarket` validates collateral token ordering, LLTV tier, and `maxLif` but never verifies that the collateral token address has deployed bytecode. Any unprivileged caller can permanently register a market whose collateral token is a codeless address. Because markets can never be deleted, the market persists on-chain indefinitely, and every `supplyCollateral` call for it unconditionally reverts with `SafeTransferLib.NoCode()`, making the market permanently inaccessible for borrowing.

## Finding Description
`touchMarket` at `src/Midnight.sol:762-773` enforces exactly three properties per `collateralParams` entry:

```solidity
require(collateralToken > previousCollateralToken, CollateralParamsNotSorted()); // sorted, non-zero
require(isLltvAllowed(lltv), LltvNotAllowed());
require(
    market.collateralParams[i].maxLif == maxLif(lltv, LIQUIDATION_CURSOR_LOW)
        || market.collateralParams[i].maxLif == maxLif(lltv, LIQUIDATION_CURSOR_HIGH),
    InvalidMaxLif()
);
```

There is no `require(collateralToken.code.length > 0)` guard. Any address above `address(0)` — including `address(type(uint160).max)` or any other address with no deployed code — satisfies the ordering check. On success, market state is permanently written at `src/Midnight.sol:775-788` (`tickSpacing`, settlement fees, continuous fee, `storeInCode`), and `MarketCreated` is emitted.

When `supplyCollateral` is subsequently called (`src/Midnight.sol:524-546`), it:
1. Calls `touchMarket` (line 528) — returns the existing id, no-op.
2. Mutates position storage (lines 532-540).
3. Calls `SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets)` at line 545.

`SafeTransferLib.safeTransferFrom` at `src/libraries/SafeTransferLib.sol:25` opens with:
```solidity
require(token.code.length > 0, NoCode());
```
Since the collateral token has no bytecode, this always reverts. The entire `supplyCollateral` transaction reverts (position mutations are rolled back), but the market itself — created by the prior `touchMarket` call — remains permanently registered. The Certora rule `marketCannotBeDeleted` (`certora/specs/CreatedMarkets.spec:82-86`) formally proves no function can ever remove a created market.

**Exploit flow:**
1. Attacker constructs a `Market` with `collateralParams[0].token = address(type(uint160).max)`, a valid LLTV (e.g. `0.77e18`), the corresponding valid `maxLif`, and any valid maturity.
2. Attacker calls `touchMarket(market)` — succeeds, `tickSpacing > 0`, market permanently registered.
3. Any call to `supplyCollateral(market, 0, assets, onBehalf)` always reverts with `NoCode()`.
4. No borrower can ever post collateral; no borrow is ever possible in this market.

## Impact Explanation
A permanently registered market with a codeless collateral token is an unrecoverable corruption of protocol state. The market exists on-chain, consumes storage, emits a `MarketCreated` event, and will be indexed by integrators and front-ends as a valid market — but no borrower can ever interact with it. Because markets are immutable after creation and provably undeletable, the state cannot be repaired. No user funds are directly frozen (lenders cannot be trapped because debt creation requires passing the health check, which requires collateral), but the protocol accumulates permanently dead market entries that cannot be cleaned up.

## Likelihood Explanation
`touchMarket` is fully permissionless — any EOA or contract can call it with arbitrary parameters. The only preconditions are a valid LLTV tier and a matching `maxLif`, both of which are publicly enumerable constants. The attack costs only gas, is trivially repeatable across any number of distinct market parameter combinations (different loan tokens, maturities, or collateral index orderings), and requires no privileged access, no capital, and no victim interaction.

## Recommendation
Add a bytecode existence check inside the `collateralParams` validation loop in `touchMarket`:

```solidity
require(collateralToken.code.length > 0, NoCode());
```

This should be placed immediately after the `collateralToken > previousCollateralToken` check at `src/Midnight.sol:764`, before the LLTV and `maxLif` checks. The same guard should be considered for the loan token (`market.loanToken`) if it is not already validated elsewhere at market creation time.

## Proof of Concept
```solidity
// Foundry test (no fork required)
function test_codelessCollateralCreatesDeadMarket() public {
    CollateralParams[] memory cp = new CollateralParams[](1);
    cp[0] = CollateralParams({
        token: address(type(uint160).max), // no bytecode on any standard chain
        lltv: 0.77e18,                     // allowed tier
        maxLif: maxLif(0.77e18, LIQUIDATION_CURSOR_LOW) // valid maxLif
    });
    Market memory market = Market({
        loanToken: address(loanToken),
        maturity: block.timestamp + 30 days,
        collateralParams: cp,
        // ... other fields
    });

    // Step 1: touchMarket succeeds — market permanently registered
    bytes32 id = midnight.touchMarket(market);
    assertGt(midnight.tickSpacing(id), 0, "market created");

    // Step 2: supplyCollateral always reverts with NoCode
    vm.expectRevert(SafeTransferLib.NoCode.selector);
    midnight.supplyCollateral(market, 0, 1e18, address(this));

    // Step 3: market still exists and cannot be deleted
    assertGt(midnight.tickSpacing(id), 0, "market still exists after failed supply");
}
```