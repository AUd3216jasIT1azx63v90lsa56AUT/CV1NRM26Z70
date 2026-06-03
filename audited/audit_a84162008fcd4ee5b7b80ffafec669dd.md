All five code references in the submitted claim have been verified against the actual source:

1. `touchMarket` loop at lines 762–773 checks only ordering, LLTV allowance, and `maxLif` — no `code.length` guard. [1](#0-0) 

2. Market state permanently written at lines 775–788 with `MarketCreated` emitted. [2](#0-1) 

3. `supplyCollateral` calls `touchMarket` at line 528, mutates position storage at lines 532–540, then calls `SafeTransferLib.safeTransferFrom` at line 545. [3](#0-2) 

4. `SafeTransferLib.safeTransferFrom` opens with `require(token.code.length > 0, NoCode())` at line 25. [4](#0-3) 

5. Certora rule `marketCannotBeDeleted` at lines 82–86 formally proves no function can remove a created market. [5](#0-4) 

---

Audit Report

## Title
Missing Bytecode Check in `touchMarket` Allows Permanent Creation of Unusable Markets - (File: src/Midnight.sol)

## Summary
`touchMarket` validates collateral ordering, LLTV tier, and `maxLif` but never checks that the collateral token address has deployed bytecode. Any unprivileged caller can permanently register a market with a codeless collateral token. Because the Certora rule `marketCannotBeDeleted` formally proves markets are undeletable, every such market persists on-chain indefinitely, and every subsequent `supplyCollateral` call unconditionally reverts with `SafeTransferLib.NoCode()`.

## Finding Description
The `touchMarket` loop at `src/Midnight.sol:762-773` enforces exactly three properties per `collateralParams` entry — ordering (`collateralToken > previousCollateralToken`), LLTV allowance (`isLltvAllowed(lltv)`), and `maxLif` validity — with no `require(collateralToken.code.length > 0)` guard.

On success, market state is permanently written at lines 775-788 (`tickSpacing`, settlement fees, continuous fee, `storeInCode`) and `MarketCreated` is emitted at line 788.

When `supplyCollateral` is subsequently called, it calls `touchMarket` at line 528 (no-op for existing market), mutates position storage at lines 532-540, then calls `SafeTransferLib.safeTransferFrom(collateralToken, ...)` at line 545. `SafeTransferLib.safeTransferFrom` opens with `require(token.code.length > 0, NoCode())` at `src/libraries/SafeTransferLib.sol:25`, which always reverts for a codeless token.

The `supplyCollateral` transaction reverts entirely (rolling back position mutations), but the market created by the prior `touchMarket` call is unaffected. The Certora rule `marketCannotBeDeleted` at `certora/specs/CreatedMarkets.spec:82-86` formally proves no function can ever remove a created market.

**Exploit flow:**
1. Attacker constructs a `Market` with `collateralParams[0].token = address(type(uint160).max)`, a valid LLTV (e.g. `0.77e18`), the corresponding valid `maxLif`, and any valid maturity.
2. Attacker calls `touchMarket(market)` — succeeds, `tickSpacing > 0`, market permanently registered.
3. Any subsequent `supplyCollateral(market, 0, assets, onBehalf)` always reverts with `NoCode()`.
4. No borrower can ever post collateral; no borrow is ever possible in this market.

## Impact Explanation
A permanently registered market with a codeless collateral token is an unrecoverable corruption of protocol state. The market exists on-chain, consumes storage, emits a `MarketCreated` event, and will be indexed by integrators and front-ends as a valid market — but no borrower can ever interact with it. Because markets are immutable after creation and provably undeletable, the state cannot be repaired. No user funds are directly frozen (lenders cannot be trapped because debt creation requires passing the health check, which requires collateral), but the protocol accumulates permanently dead market entries that cannot be cleaned up. This matches the `RESEARCHER.md` impact class of "Permanent lock, freeze, or unrecoverable corruption of user/project state."

## Likelihood Explanation
`touchMarket` is fully permissionless — any EOA or contract can call it with arbitrary parameters. The only preconditions are a valid LLTV tier and a matching `maxLif`, both of which are publicly enumerable constants. The attack costs only gas, is trivially repeatable across any number of distinct market parameter combinations (different loan tokens, maturities, or collateral index orderings), and requires no privileged access, no capital, and no victim interaction.

## Recommendation
Add a bytecode existence check inside the `touchMarket` collateral loop, immediately after resolving `collateralToken`:

```solidity
require(collateralToken.code.length > 0, NoCode());
```

This mirrors the guard already present in `SafeTransferLib.safeTransferFrom` and ensures that only tokens with deployed bytecode can anchor a market at creation time, preventing permanently dead market entries.

## Proof of Concept
```solidity
// Minimal Foundry test
function testPermanentDeadMarket() public {
    address codelessToken = address(uint160(type(uint160).max));
    // Confirm no bytecode
    assertEq(codelessToken.code.length, 0);

    uint256 lltv = 0.77e18; // must be an allowed LLTV tier
    Midnight.CollateralParams[] memory cp = new Midnight.CollateralParams[](1);
    cp[0] = Midnight.CollateralParams({
        token: codelessToken,
        lltv: lltv,
        maxLif: midnight.maxLif(lltv, midnight.LIQUIDATION_CURSOR_LOW())
    });
    Midnight.Market memory market = Midnight.Market({
        loanToken: address(loanToken),
        collateralParams: cp,
        maturity: block.timestamp + 365 days
    });

    // Step 1: touchMarket succeeds — market permanently registered
    bytes32 id = midnight.touchMarket(market);
    assertGt(midnight.marketState(id).tickSpacing, 0);

    // Step 2: supplyCollateral always reverts with NoCode()
    vm.expectRevert(SafeTransferLib.NoCode.selector);
    midnight.supplyCollateral(market, 0, 1e18, address(this));

    // Step 3: market still exists — cannot be deleted
    assertGt(midnight.marketState(id).tickSpacing, 0);
}
```

### Citations

**File:** src/Midnight.sol (L528-545)
```text
        bytes32 id = touchMarket(market);
        address collateralToken = market.collateralParams[collateralIndex].token;

        Position storage _position = position[id][onBehalf];
        uint256 oldCollateral = _position.collateral[collateralIndex];
        _position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets);

        if (oldCollateral == 0 && assets > 0) {
            uint128 newCollateralBitmap = _position.collateralBitmap.setBit(collateralIndex);
            _position.collateralBitmap = newCollateralBitmap;
            require(
                UtilsLib.countBits(newCollateralBitmap) <= MAX_COLLATERALS_PER_BORROWER, TooManyActivatedCollaterals()
            );
        }

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

**File:** src/Midnight.sol (L775-788)
```text
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
```

**File:** src/libraries/SafeTransferLib.sol (L24-25)
```text
    function safeTransferFrom(address token, address from, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());
```

**File:** certora/specs/CreatedMarkets.spec (L82-86)
```text
rule marketCannotBeDeleted(env e, method f, calldataarg args, Midnight.Market market) {
    require marketIsCreated(market), "Assume that the market is created";
    f(e, args);
    assert marketIsCreated(market);
}
```
