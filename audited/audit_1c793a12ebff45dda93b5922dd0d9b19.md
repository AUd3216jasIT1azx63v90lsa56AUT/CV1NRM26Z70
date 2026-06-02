Audit Report

## Title
Off-by-one `<=` in `CannotIncreaseDebtPostMaturity` guard permits debt creation at exact maturity timestamp - (File: `src/Midnight.sol`)

## Summary
The guard at `src/Midnight.sol:391` uses `block.timestamp <= offer.market.maturity`, which evaluates to `true` when `block.timestamp == market.maturity`, allowing `sellerDebtIncrease > 0` to pass through at the exact maturity second. This violates the protocol's core invariant that debt must not be created at or after maturity. The newly created debt accrues zero continuous fee (because `timeToMaturity == 0`) and cannot be liquidated via the post-maturity path until the next block.

## Finding Description
**Root cause:** `src/Midnight.sol:391` uses `<=` instead of `<`:
```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
At `block.timestamp == market.maturity`, the left operand is `true`, so the `require` passes unconditionally regardless of `sellerDebtIncrease`.

**Exploit flow:**

1. `src/Midnight.sol:359` computes `timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp)` → yields `0` at equality.
2. `src/Midnight.sol:971` routes `timeToMaturity < 1 days` to the 0d breakpoint, returning `settlementFeeCbp0 * CBP`.
3. `src/Midnight.sol:386` computes `buyerPendingFeeIncrease = buyerCreditIncrease.mulDivDown(_marketState.continuousFee * 0, WAD)` → `0`. No continuous fee is charged on new credit.
4. `src/Midnight.sol:384` computes `sellerDebtIncrease = units - sellerCreditDecrease`, which is `> 0` when the seller has no existing credit.
5. `src/Midnight.sol:391` — the guard passes because `block.timestamp == market.maturity` satisfies `<=`.
6. `src/Midnight.sol:414` writes `sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease)` to storage.
7. `src/Midnight.sol:476` checks health only after the debt is written; a seller with sufficient collateral passes.

**Why existing checks fail:**
- The `CannotIncreaseDebtPostMaturity` guard is the only maturity-boundary enforcement for debt creation; its `<=` makes it ineffective at the exact maturity second.
- The `SellerIsLiquidatable` check at line 476 enforces collateral health, not the maturity boundary — a well-collateralized seller passes.
- Post-maturity liquidation at `src/Midnight.sol:622` requires `block.timestamp > market.maturity` (strict `>`), so at exactly maturity this path is unavailable. The debt exists but cannot be liquidated via the post-maturity route until the next block.

**Dual-boundary inconsistency:** At `block.timestamp == market.maturity`, the protocol simultaneously treats the timestamp as pre-maturity for the debt-increase guard (`<=` passes) and as post-maturity for fee accounting (`timeToMaturity == 0`, zero continuous fee on new credit).

## Impact Explanation
Debt is created at the exact maturity second in violation of the core invariant. The buyer's credit position carries zero `pendingFee` obligation despite being created at the protocol's terminal boundary. For one block, the debt exists with no available post-maturity liquidation path. If collateral value drops in that window, the position can become bad debt before any liquidator can act under post-maturity mode, resulting in a concrete loss of lender funds.

## Likelihood Explanation
Any unprivileged taker can trigger this by calling `take()` with a sell offer at a block where `block.timestamp == market.maturity`. On Ethereum, validator-influenced timestamps make exact-second targeting feasible; on L2s (Arbitrum, Base) sequencers set timestamps with fine granularity, making this straightforward. The precondition — seller has collateral and no existing credit — is easily arranged. The attack is repeatable across any market whose maturity falls on a reachable block timestamp.

## Recommendation
Change `<=` to `<` at `src/Midnight.sol:391`:
```solidity
// Before
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());

// After
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
This aligns the debt-increase guard with the post-maturity liquidation condition (`block.timestamp > market.maturity` at line 622), eliminating the one-second gap where debt creation is permitted but post-maturity liquidation is not yet available.

## Proof of Concept
```solidity
// Minimal Foundry test
function test_debtCreatedAtExactMaturity() public {
    // Setup: create market with maturity = T
    // Fund seller with collateral (no existing credit)
    // Warp to block.timestamp == market.maturity (NOT maturity + 1)
    vm.warp(market.maturity);

    uint256 debtBefore = midnight.position(id, seller).debt;
    // Taker calls take() with a sell offer
    vm.prank(taker);
    midnight.take(offer, units, ...);
    uint256 debtAfter = midnight.position(id, seller).debt;

    // Asserts debt was created at maturity — should revert but does not
    assertGt(debtAfter, debtBefore);
    // Confirm post-maturity liquidation is unavailable at this timestamp
    vm.expectRevert(NotLiquidatable.selector);
    midnight.liquidate(market, seller, ...);
}
```
The test warps to exactly `market.maturity` (not `maturity + 1` as all existing tests do), fills a sell offer with a seller having no prior credit, and asserts that debt is written to storage while post-maturity liquidation reverts.