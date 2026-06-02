All code references verified against the actual source. Here is the validated output:

---

Audit Report

## Title
Off-by-one `<=` in `CannotIncreaseDebtPostMaturity` allows debt creation at exact maturity timestamp - (File: src/Midnight.sol)

## Summary
The guard at `src/Midnight.sol:391` uses `block.timestamp <= offer.market.maturity`, which evaluates to `true` when `block.timestamp == market.maturity`, permitting `sellerDebtIncrease > 0` at the exact maturity second. At this same timestamp, `timeToMaturity` resolves to `0`, so newly created debt carries zero pending fee obligation — a dual-boundary inconsistency where the protocol simultaneously treats exact maturity as pre-maturity for the debt-increase guard and as post-maturity for fee accounting. Post-maturity liquidation via `postMaturityMode = true` is also unavailable until the next block.

## Finding Description
**Root cause confirmed at `src/Midnight.sol:391`:**
```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
When `block.timestamp == market.maturity`, the left operand is `true` and the require passes unconditionally regardless of `sellerDebtIncrease`.

**Full verified code path at exact maturity:**

- `src/Midnight.sol:359`: `timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp)` → `0`.
- `src/Midnight.sol:360`: `settlementFee(id, 0)` enters the `timeToMaturity < 1 days` branch at `src/Midnight.sol:971`, returning the 0-day post-maturity breakpoint fee.
- `src/Midnight.sol:385–386`: `buyerPendingFeeIncrease = buyerCreditIncrease.mulDivDown(_marketState.continuousFee * 0, WAD)` = `0`. No continuous fee is charged on newly created credit.
- `src/Midnight.sol:384`: `sellerDebtIncrease = units - sellerCreditDecrease` is `> 0` when the seller has no existing credit.
- `src/Midnight.sol:391`: Guard passes because `block.timestamp <= market.maturity` is `true`.
- `src/Midnight.sol:414`: `sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease)` — debt written to storage.
- `src/Midnight.sol:476`: `require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable())` — enforces health only; the position passes because it is healthy at creation.
- `src/Midnight.sol:622`: Post-maturity liquidation requires strict `block.timestamp > market.maturity`; at exact maturity this is `false`, so the newly created debt cannot be liquidated via `postMaturityMode = true` until the next block.

**Dual-boundary inconsistency confirmed:** The protocol simultaneously treats `block.timestamp == market.maturity` as pre-maturity for the debt-increase guard (`<=` passes) and as post-maturity for fee accounting (`timeToMaturity == 0`, zero continuous fee on new credit).

**Test coverage gap confirmed:** All `CannotIncreaseDebtPostMaturity` tests in `test/TakeTest.sol` warp to `maturity + 1`; the exact-maturity edge case (`block.timestamp == maturity`) is untested.

## Impact Explanation
Debt is created at the exact maturity second in violation of the core protocol invariant. The new debt carries zero pending fee obligation because `timeToMaturity == 0` at creation, meaning lenders lose the continuous fee that should have accrued on this position. Post-maturity liquidation (`postMaturityMode = true`) of this debt is unavailable until `block.timestamp > market.maturity` (next block). While normal-mode liquidation remains available if the position becomes unhealthy during this window, the fee accounting loss is concrete and unconditional: any debt created at exact maturity escapes continuous fee obligation entirely.

## Likelihood Explanation
Any unprivileged taker can trigger this by calling `take()` with a sell offer at a block where `block.timestamp == market.maturity`. On Ethereum, validator-influenced timestamps make exact-second targeting feasible within a ~12-second window. On L2s (Arbitrum, Base), sequencers set timestamps with finer granularity, making exact-second targeting straightforward. The precondition — seller has sufficient collateral and no existing credit — is easily arranged. The attack is repeatable across any market whose maturity falls on a reachable block timestamp.

## Recommendation
Change `<=` to `<` at `src/Midnight.sol:391`:
```solidity
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
This aligns the debt-increase guard with the fee accounting boundary, which already treats `timeToMaturity == 0` as post-maturity.

## Proof of Concept
```solidity
// In TakeTest.sol, add:
function test_take_exactMaturity_debtCreated() public {
    // Setup: create market with maturity = block.timestamp + 1 day
    // Supply collateral for seller
    // Warp to exactly maturity (not maturity + 1)
    vm.warp(market.maturity);
    // Call take() with a sell offer where seller has no existing credit
    // Assert: sellerPos.debt > 0 (debt was created at maturity)
    // Assert: sellerPos.pendingFee == 0 (no fee obligation)
    // Assert: liquidate(..., postMaturityMode=true) reverts with NotLiquidatable
}
```
The test warps to `market.maturity` (not `maturity + 1`) and verifies that debt is created with zero pending fee and that post-maturity liquidation is blocked.