### Title
Codeless Collateral Token Accepted by `touchMarket` Creates Permanently Unusable Markets - (File: src/Midnight.sol)

### Summary
`touchMarket` validates collateral token ordering, LLTV, and maxLif but never checks that the collateral token address has deployed bytecode. Any unprivileged caller can create a market with `token = address(type(uint160).max)` — which satisfies `collateralToken > address(0)` — and `touchMarket` succeeds. Once created, the market is permanently registered but every subsequent `supplyCollateral` call reverts with `SafeTransferLib.NoCode()`, making the market permanently unusable for borrowing.

### Finding Description
In `src/Midnight.sol:762-773`, `touchMarket` iterates over `collateralParams` and enforces exactly three properties per entry:

```
collateralToken > previousCollateralToken   // sorted, no duplicates, no address(0)
isLltvAllowed(lltv)
maxLif == maxLif(lltv, CURSOR_LOW) || maxLif == maxLif(lltv, CURSOR_HIGH)
```

There is no `require(collateralToken.code.length > 0)` check. `address(type(uint160).max)` satisfies `collateralToken > address(0)`, so with a valid lltv and maxLif the call succeeds and the market is permanently written to state at `src/Midnight.sol:775-788`. Markets cannot be deleted (enforced by the `marketCannotBeDeleted` Certora invariant).

When `supplyCollateral` is subsequently called (`src/Midnight.sol:524-546`), it writes position state at lines 532-540 and then calls:

```solidity
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
```

`SafeTransferLib.safeTransferFrom` at `src/libraries/SafeTransferLib.sol:25` opens with:

```solidity
require(token.code.length > 0, NoCode());
```

Since `address(type(uint160).max)` has no deployed bytecode on any standard EVM chain, this always reverts. The entire transaction reverts (no state corruption), but the market remains permanently created and permanently inaccessible for borrowing.

**Exploit flow:**
1. Attacker constructs `Market` with `collateralParams[0].token = address(type(uint160).max)`, valid lltv (e.g. `0.77e18`), valid maxLif, valid maturity.
2. Attacker calls `touchMarket(market)` — succeeds, `tickSpacing > 0`, market permanently registered.
3. Any call to `supplyCollateral(market, 0, assets, onBehalf)` always reverts with `NoCode()`.
4. No borrower can ever post collateral; no borrow is ever possible in this market.

The same applies to any address in `[address(1), address(type(uint160).max)]` that has no deployed code, which is the vast majority of that range on any live chain.

### Impact Explanation
A market with a codeless collateral token is permanently created and permanently unusable for borrowing. The market cannot be deleted. This is a concrete malformed protocol state: the market exists on-chain, consumes storage, emits a `MarketCreated` event, and is indexed by integrators, but no borrower can ever interact with it. Lenders cannot be financially trapped because borrowers cannot take buy offers without passing the health check (which requires collateral), but the market is a permanent dead entry.

### Likelihood Explanation
`touchMarket` is permissionless — any address can call it with arbitrary parameters. The only preconditions are a valid lltv tier and a valid maxLif, both of which are publicly enumerable constants. The attack is trivially repeatable at negligible cost (gas only) and can be executed for any number of distinct market parameter combinations.

### Recommendation
Add a bytecode existence check for each collateral token inside the loop in `touchMarket`, immediately after the sort check:

```solidity
require(collateralToken > previousCollateralToken, CollateralParamsNotSorted());
require(collateralToken.code.length > 0, NoCode()); // add this
```

`SafeTransferLib.NoCode` is already defined and used in `src/libraries/SafeTransferLib.sol:8`, so no new error type is needed.

### Proof of Concept

```solidity
function testFuzzCodelessCollateralTokenMarketCreation(address token) public {
    // Precondition: any non-zero address with no code
    vm.assume(token > address(0));
    vm.assume(token.code.length == 0);

    CollateralParams[] memory params = new CollateralParams[](1);
    params[0] = CollateralParams({
        token: token,
        lltv: 0.77e18,
        maxLif: maxLif(0.77e18, 0.25e18),
        oracle: address(oracle1)
    });

    Market memory m;
    m.loanToken = address(loanToken);
    m.maturity = block.timestamp + 100;
    m.collateralParams = params;

    // touchMarket succeeds — no code check
    bytes32 id = midnight.touchMarket(m);
    assertGt(midnight.tickSpacing(id), 0, "market permanently created");

    // supplyCollateral always reverts — market permanently unusable
    vm.expectRevert(SafeTransferLib.NoCode.selector);
    midnight.supplyCollateral(m, 0, 1e18, address(this));
}
```

**Expected assertions:**
- `touchMarket` returns a non-zero id and `tickSpacing > 0` for any codeless token address above `address(0)`.
- `supplyCollateral` reverts with `SafeTransferLib.NoCode.selector` for every such address.
- Fuzzer confirms the property holds across the full range `[address(1), address(type(uint160).max)]` for addresses without code. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** src/Midnight.sol (L543-545)
```text
        emit EventsLib.SupplyCollateral(msg.sender, id, collateralToken, assets, onBehalf);

        SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
```

**File:** src/Midnight.sol (L762-773)
```text
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

**File:** src/libraries/SafeTransferLib.sol (L24-25)
```text
    function safeTransferFrom(address token, address from, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());
```
