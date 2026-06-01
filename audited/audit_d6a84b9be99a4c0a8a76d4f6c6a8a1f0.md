Looking at the actual `touchMarket` implementation and the `isHealthy` function in `src/Midnight.sol`, plus the `EcrecoverAuthorizer.sol` file (which is a periphery authorization contract unrelated to oracle validation), I can now analyze the claim.

**Key findings:**

`touchMarket` (lines 755–791) validates: sorted/non-zero collateral tokens, allowed LLTV tiers, valid maxLif, maturity bounds. It does **not** validate `collateralParams[i].oracle`.

`isHealthy` (lines 944–960) calls `IOracle(collateralParam.oracle).price()` for every activated collateral when debt > 0. Calling this on `address(0)` (no code) causes the low-level call to return empty bytes; Solidity 0.8.x then fails to ABI-decode the expected `uint256`, reverting.

The Certora invariant `createdMarketsHaveNonZeroCollaterals` only checks `token != 0`, not `oracle != 0`. No on-chain check fills this gap.

---

### Title
Missing oracle address validation in `touchMarket` allows permanent position freeze - (`src/Midnight.sol`)

### Summary
`touchMarket` validates collateral token addresses, LLTV tiers, and maxLif values but performs no check that `collateralParams[i].oracle != address(0)`. Any unprivileged caller can create a market with a zero oracle address for one or more collateral indices. Any borrower who subsequently activates that collateral index and carries debt will have `isHealthy`, `withdrawCollateral`, and `liquidate` permanently revert, freezing both their collateral and their debt.

### Finding Description
**Root cause:** In `src/Midnight.sol`, `touchMarket` (lines 762–773) iterates over `collateralParams` and checks `collateralToken > previousCollateralToken` (sorted/non-zero token), `isLltvAllowed(lltv)`, and `maxLif` validity. There is no `require(market.collateralParams[i].oracle != address(0))` check.

**Exploit flow:**
1. Attacker (any address) calls `touchMarket` with a `Market` where `collateralParams[0].token = address(someValidToken)`, `collateralParams[0].oracle = address(0)`, valid LLTV and maxLif. The call succeeds; the market is created.
2. Victim calls `supplyCollateral(market, 0, amount, victim)` — oracle is never called here; the collateral bit is set in `_position.collateralBitmap`.
3. Victim borrows (via `take`), acquiring non-zero `_position.debt`.
4. Any subsequent call that reaches `isHealthy` with `debt > 0` enters the bitmap loop (line 950), reads `collateralParam.oracle == address(0)`, and executes `IOracle(address(0)).price()`. Since `address(0)` has no code, the call returns empty bytes; Solidity's ABI decoder reverts trying to decode a `uint256` from zero bytes.
5. `withdrawCollateral` (line 568: `require(isHealthy(...))`) reverts. `liquidate` (which calls `isHealthy` internally) reverts. The direct `isHealthy` view reverts.

**Why existing checks fail:** The sorted-token check (`collateralToken > previousCollateralToken`) only prevents `token == address(0)` and duplicates; it says nothing about the `oracle` field. No other check in `touchMarket` or anywhere else in the creation path validates the oracle address.

### Impact Explanation
Any borrower who activates the poisoned collateral index and holds debt has their position permanently frozen: they cannot withdraw collateral, cannot be liquidated (their debt cannot be recovered), and `isHealthy` always reverts for them. Their collateral is locked in the contract with no recovery path, and their debt accrues indefinitely. This violates the core invariants that collateral cannot be seized/withdrawn outside health/liquidation rules and that unhealthy positions remain liquidatable.

### Likelihood Explanation
The precondition is trivially achievable: `touchMarket` is permissionless, requires no tokens, and the only constraint on the oracle field is that it be ABI-encodable as an address. A single transaction creates the trap. Victims are any borrowers who interact with the market without inspecting the raw `collateralParams[i].oracle` field off-chain. The freeze is permanent and irreversible once a borrower has debt with the activated index.

### Recommendation
Add an explicit non-zero oracle check inside the `touchMarket` collateral validation loop:

```solidity
require(market.collateralParams[i].oracle != address(0), OracleNotSet());
```

Optionally, also perform a sanity call (`IOracle(market.collateralParams[i].oracle).price()`) at market creation time to verify the oracle is callable, though the zero-address check is the minimum necessary fix.

### Proof of Concept
```solidity
function testZeroOracleFreezesBorrower() public {
    // 1. Create market with oracle = address(0)
    CollateralParams[] memory params = new CollateralParams[](1);
    params[0] = CollateralParams({
        token: address(collateralToken1),
        lltv: 0.77e18,
        maxLif: maxLif(0.77e18, 0.25e18),
        oracle: address(0)          // <-- zero oracle
    });
    Market memory m;
    m.loanToken = address(loanToken);
    m.maturity = block.timestamp + 100;
    m.collateralParams = params;

    // 2. Market creation succeeds (no oracle validation)
    bytes32 id = midnight.touchMarket(m);
    assertGt(midnight.tickSpacing(id), 0, "market created");

    // 3. Supply collateral (oracle not called)
    deal(address(collateralToken1), borrower, 1e18);
    vm.prank(borrower);
    collateralToken1.approve(address(midnight), 1e18);
    midnight.supplyCollateral(m, 0, 1e18, borrower);

    // 4. Borrower acquires debt (via take / setupMarket)
    setupMarket(m, 1e18);   // lender provides liquidity, borrower takes

    // 5. isHealthy reverts because IOracle(address(0)).price() reverts
    vm.expectRevert();
    midnight.isHealthy(m, id, borrower);

    // 6. withdrawCollateral reverts
    vm.prank(borrower);
    vm.expectRevert();
    midnight.withdrawCollateral(m, 0, 1, borrower, borrower);

    // 7. liquidate reverts
    vm.expectRevert();
    midnight.liquidate(m, 0, 0, 1, borrower, false, address(this), address(0), "");
}
```

Expected: all three `vm.expectRevert()` assertions pass, confirming the position is permanently frozen. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** src/Midnight.sol (L944-960)
```text
    function isHealthy(Market memory market, bytes32 id, address borrower) public view returns (bool) {
        Position storage _position = position[id][borrower];
        uint256 debt = _position.debt;
        uint256 maxDebt;
        if (debt > 0) {
            uint128 _collateralBitmap = _position.collateralBitmap;
            while (_collateralBitmap != 0) {
                uint256 i = UtilsLib.msb(_collateralBitmap);
                CollateralParams memory collateralParam = market.collateralParams[i];
                uint256 price = IOracle(collateralParam.oracle).price();
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
                _collateralBitmap = _collateralBitmap.clearBit(i);
            }
        }
        return maxDebt >= debt;
    }
```

**File:** certora/specs/CreatedMarkets.spec (L65-67)
```text
// Show that a created market do not have address(0) collateralParams.
strong invariant createdMarketsHaveNonZeroCollaterals(Midnight.Market market, uint256 i)
    marketIsCreated(market) => i < market.collateralParams.length => market.collateralParams[i].token != 0;
```
