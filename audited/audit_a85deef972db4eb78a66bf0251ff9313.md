All key claims verified against the actual code. Here is the validation output:

---

Audit Report

## Title
Missing `loanToken` validation in `touchMarket` allows permanent creation of a broken market where `take()` always reverts - (File: src/Midnight.sol)

## Summary
`touchMarket` performs no check that `market.loanToken != address(0)`, allowing any unprivileged caller to permanently register a market with `loanToken == address(0)`. Once created, `marketState[id].tickSpacing` is set to `DEFAULT_TICK_SPACING` and cannot be unset. Every subsequent call to `take()` on this market unconditionally reaches `SafeTransferLib.safeTransferFrom(address(0), ...)`, which reverts at the `require(token.code.length > 0, NoCode())` guard, permanently blocking all trading in that market.

## Finding Description
**Root cause — `touchMarket` (Midnight.sol:755–791):**

The function validates `market.maturity` (line 758), `collateralParams.length` (lines 759–760), collateral token ordering via strict `>` comparison (line 764, which incidentally blocks `address(0)` as a *collateral* token since `previousCollateralToken` starts at `address(0)`, but says nothing about `loanToken`), per-collateral `lltv` (line 766), and `maxLif` (lines 767–771). There is no `require(market.loanToken != address(0))` or equivalent check. The market state is written unconditionally at line 776:

```solidity
_marketState.tickSpacing = DEFAULT_TICK_SPACING;
```

**Exploit flow:**

1. Attacker constructs a `Market` struct with `loanToken = address(0)` and otherwise valid collateral params (sorted, valid LLTV/maxLif, valid maturity).
2. Attacker calls `midnight.touchMarket(market)` — all checks pass, `marketState[id].tickSpacing` is set to `DEFAULT_TICK_SPACING` (non-zero). Market is permanently registered.
3. Any caller invokes `take()` on this market. `take()` calls `touchMarket` at line 347 — a no-op since `tickSpacing > 0`. Execution proceeds through all validation and accounting logic, then reaches:

```solidity
// Midnight.sol:455–456
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

4. `SafeTransferLib.safeTransferFrom(address(0), ...)` immediately reverts at:

```solidity
// SafeTransferLib.sol:25
require(token.code.length > 0, NoCode());
```

`address(0).code.length == 0` always, so this revert is unconditional.

**Correction to claimed secondary impact:** The claim that collateral can be permanently locked is incorrect. `supplyCollateral` (line 545) uses the collateral token, not `loanToken`, so it succeeds. `withdrawCollateral` (line 572) also uses the collateral token. Since debt can only be created via `take()` (confirmed by the Certora spec at `certora/specs/BalanceEffects.spec:188–203`), and `take()` always reverts on this market, no debt can ever be created, so `withdrawCollateral` always passes the `isHealthy` check. Collateral is not permanently locked.

**Why existing protections are insufficient:**

The `SafeTransferLib.NoCode` guard correctly prevents the ERC-20 call from succeeding, but it fires *after* the market has already been permanently created in a prior transaction. The market ID is a deterministic hash of the market parameters; once `tickSpacing > 0` is set for that ID, no mechanism exists to delete or reset it.

## Impact Explanation
The market with `loanToken == address(0)` is permanently registered with `tickSpacing > 0`. Every invocation of `take()` on this market reverts with `NoCode()`, permanently blocking all trading. `withdraw()` (line 499) and `repay()` (line 520) are similarly broken on this market, though they are unreachable in practice since no credit or debt can be created without a successful `take()`. The permanent, irreversible corruption of a market's state — rendering it permanently non-functional — constitutes unrecoverable corruption of protocol state, which is an in-scope impact per RESEARCHER.md.

## Likelihood Explanation
The precondition is trivially satisfiable: `touchMarket` is a public function with no access control. Constructing a `Market` struct with `loanToken = address(0)` and valid collateral params requires no special privilege. The attack is a single transaction. It is repeatable for any market ID not yet created. The only constraint is supplying valid collateral params (sorted, valid LLTV/maxLif, valid maturity), all of which are publicly known allowed values.

## Recommendation
Add an explicit non-zero check for `loanToken` at the start of the market initialization block in `touchMarket`:

```solidity
require(market.loanToken != address(0), InvalidLoanToken());
```

This should be placed alongside the existing parameter validation checks (lines 758–771) before any state is written.

## Proof of Concept
```solidity
function testBrokenMarketWithZeroLoanToken() public {
    // 1. Construct a market with loanToken = address(0) and valid collateral params
    Market memory badMarket;
    badMarket.loanToken = address(0);
    badMarket.maturity = block.timestamp + 100;
    badMarket.collateralParams.push(CollateralParams({
        token: address(collateralToken1), // must be > address(0)
        lltv: 0.77e18,
        maxLif: maxLif(0.77e18, 0.25e18),
        oracle: address(oracle1)
    }));

    // 2. touchMarket succeeds — market is permanently created
    bytes32 badId = midnight.touchMarket(badMarket);
    assertGt(midnight.marketState(badId).tickSpacing, 0); // market is registered

    // 3. Any take() on this market reverts with NoCode()
    Offer memory offer = /* construct valid offer for badMarket */;
    vm.expectRevert(SafeTransferLib.NoCode.selector);
    midnight.take(offer, hex"", 1e18, address(this), address(this), address(0), hex"");

    // 4. Market cannot be unregistered — tickSpacing remains non-zero forever
    assertGt(midnight.marketState(badId).tickSpacing, 0);
}
```