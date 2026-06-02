The code confirms all material claims. Here is the validated output:

Audit Report

## Title
Missing Minimum Maturity Floor in `touchMarket` Allows Permanent Freeze of Borrow Side via Pre-Expired Market Creation - (File: src/Midnight.sol)

## Summary
`touchMarket` enforces only an upper-bound check on `market.maturity` but no lower-bound, allowing any unprivileged caller to create a market with `maturity < block.timestamp`. Because `take` unconditionally reverts with `CannotIncreaseDebtPostMaturity` whenever `block.timestamp > market.maturity && sellerDebtIncrease > 0`, and because market configuration is immutable after creation, the borrow side of any such market is permanently frozen from the moment of creation with no recovery path.

## Finding Description

**Root cause — `touchMarket` (`src/Midnight.sol:757-758`):**

```solidity
if (marketState[id].tickSpacing == 0) {
    require(market.maturity <= block.timestamp + 100 * 365 days, MaturityTooFar());
```

Only a ceiling is enforced. No `require(market.maturity >= block.timestamp)` or equivalent floor check exists. A value of `0` or `block.timestamp - 1` passes silently.

`touchMarket` is declared `public` with no access control (`src/Midnight.sol:755`), making it callable by any external account.

**Trigger — `take` (`src/Midnight.sol:391`):**

```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

With `maturity < block.timestamp` from inception, `block.timestamp <= maturity` is always `false`. Any `take` where `sellerDebtIncrease > 0` reverts immediately and permanently.

`sellerDebtIncrease` is computed at `src/Midnight.sol:384` as `units - sellerCreditDecrease`. For a fresh borrower with no existing credit, `sellerCreditDecrease == 0` and `sellerDebtIncrease == units > 0`, guaranteeing the revert.

**Immutability:** Once `tickSpacing != 0`, the creation block in `touchMarket` is skipped entirely (`src/Midnight.sol:757`). The maturity is embedded in the market ID via `IdLib.storeInCode(market, INITIAL_CHAIN_ID)` (`src/Midnight.sol:786`) and cannot be changed.

**Exploit flow:**
1. Attacker calls `touchMarket` with `market.maturity = block.timestamp - 1` and otherwise valid parameters.
2. Market is created successfully; `tickSpacing` is set, fees are copied, market is stored immutably.
3. Any subsequent `take` with a sell offer where `sellerDebtIncrease > 0` reverts with `CannotIncreaseDebtPostMaturity`.
4. The market is permanently in a post-maturity state with no admin override or recovery path.

**Existing checks are insufficient:** The only maturity validation in `touchMarket` is the `MaturityTooFar` upper-bound guard at line 758. No other function in the creation path validates that maturity is in the future.

## Impact Explanation

The borrow side of any market created this way is permanently frozen from inception. No borrower can ever increase debt in this market. Any lender whose signed offer references this market ID will have that offer permanently unfillable on the debt-increasing path. Because market config is immutable and `touchMarket` is permissionless, an attacker can create an unbounded number of permanently broken markets for any loan token / collateral combination at the cost of gas only. This constitutes a permanent, unrecoverable freeze of the borrow-side fund flow for affected markets, matching the "Permanent lock, freeze, or unrecoverable corruption of user/project state" impact class in RESEARCHER.md.

## Likelihood Explanation

Preconditions: none beyond being able to call a public function. The attacker requires no privileged keys, no governance access, and no special role. The attack is trivially repeatable for any loan token / collateral combination. The existing fuzz test `testPostMaturitySettlementFee` in `test/SettlementFeeTest.sol` already exercises this exact path (`maturity = bound(maturity, 0, vm.getBlockTimestamp() - 1)`), confirming the revert is reachable and reproducible.

## Recommendation

Add a minimum maturity floor check inside the market-creation branch of `touchMarket`, immediately after or alongside the existing upper-bound check:

```solidity
require(market.maturity >= block.timestamp, MaturityInPast());
require(market.maturity <= block.timestamp + 100 * 365 days, MaturityTooFar());
```

This ensures no market can be created in an already-expired state. The check must be inside the `if (marketState[id].tickSpacing == 0)` block so it only applies at creation time, consistent with the existing upper-bound guard.

## Proof of Concept

Minimal Foundry test:

```solidity
function testPreExpiredMarketPermanentlyFreezesBorrowSide() public {
    // 1. Build a market struct with maturity = block.timestamp - 1
    Market memory market = _buildValidMarket();
    market.maturity = block.timestamp - 1;

    // 2. Any unprivileged caller creates the market
    vm.prank(attacker);
    bytes32 id = midnight.touchMarket(market);

    // 3. Confirm market was created (tickSpacing != 0)
    assertGt(midnight.marketState(id).tickSpacing, 0);

    // 4. Attempt a borrow-side take; must revert with CannotIncreaseDebtPostMaturity
    Offer memory offer = _buildSellOffer(market);
    vm.prank(borrower);
    vm.expectRevert(CannotIncreaseDebtPostMaturity.selector);
    midnight.take(offer, ratifierData, 1e18, borrower, borrower, "");
}
```

The existing fuzz harness `testPostMaturitySettlementFee` in `test/SettlementFeeTest.sol` already bounds maturity to `[0, block.timestamp - 1]` and exercises the same revert path, providing a ready-made reproduction vehicle.