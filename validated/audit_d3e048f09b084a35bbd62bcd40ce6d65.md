All code references are confirmed against the actual source. Here is the validation result:

---

Audit Report

## Title
Off-by-one `<=` in `CannotIncreaseDebtPostMaturity` guard permits debt creation at exact maturity timestamp - (File: `src/Midnight.sol`)

## Summary
The guard at `src/Midnight.sol:391` uses `block.timestamp <= offer.market.maturity`, which evaluates to `true` at the exact maturity second. This allows `sellerDebtIncrease > 0` to pass the check, writing new debt to storage with zero `pendingFee` (because `timeToMaturity == 0` zeroes the continuous-fee term). Simultaneously, post-maturity liquidation is unavailable for that block because it requires the strict `block.timestamp > market.maturity`.

## Finding Description

**Root cause — line 391:**
```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
At `block.timestamp == offer.market.maturity`, the left operand is `true`, so the `require` passes unconditionally regardless of `sellerDebtIncrease`.

**Exploit flow (all lines confirmed in source):**

1. `timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp)` → `0` at equality (line 359).
2. `buyerPendingFeeIncrease = buyerCreditIncrease.mulDivDown(_marketState.continuousFee * 0, WAD)` → `0`. No continuous fee is charged on new credit (lines 385–386).
3. If the seller has no existing credit, `sellerCreditDecrease = min(units, 0) = 0`, so `sellerDebtIncrease = units > 0` (lines 383–384).
4. The guard at line 391 passes; `sellerPos.debt += sellerDebtIncrease` is written to storage (line 414).
5. Post-maturity liquidation at line 622 requires `block.timestamp > market.maturity` (strict `>`); at exactly maturity this path is closed for the entire block.
6. Pre-maturity liquidation requires `originalDebt > maxDebt` (line 622), but the `isHealthy` check at line 476 ensures the seller is healthy at take-time, so neither liquidation path is available for the newly created debt during that block.

**Why existing checks fail:**
- `CannotIncreaseDebtPostMaturity` is the sole maturity-boundary enforcement for debt creation; its `<=` makes it ineffective at the exact maturity second.
- `SellerIsLiquidatable` at line 476 enforces collateral health, not the maturity boundary — a well-collateralized seller passes.
- The dual-boundary inconsistency is concrete: at `block.timestamp == market.maturity`, the protocol simultaneously treats the timestamp as pre-maturity for the debt-increase guard (`<=` passes) and as post-maturity for fee accounting (`timeToMaturity == 0`, zero continuous fee on new credit).

## Impact Explanation
Debt is created at the exact maturity second in violation of the core protocol invariant. The buyer's credit position carries zero `pendingFee` despite being created at the terminal boundary, representing an accounting loss to lenders. For one full block, the newly created debt cannot be liquidated via either the post-maturity path (strict `>` required) or the pre-maturity path (position is healthy by construction). If collateral value drops in that window (e.g., oracle update in the same or next block), the position can become bad debt before any liquidator can act, resulting in a concrete loss of lender funds.

## Likelihood Explanation
Any unprivileged taker can trigger this by calling `take()` with a sell offer at a block where `block.timestamp == offer.market.maturity`. On Ethereum, validator-influenced timestamps make exact-second targeting feasible; on L2s (Arbitrum, Base) sequencers set timestamps with fine granularity, making this straightforward. The precondition — seller has collateral and no existing credit — is easily arranged. The attack is repeatable across any market whose maturity falls on a reachable block timestamp.

## Recommendation
Change the guard at line 391 from `<=` to `<`:
```solidity
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
This aligns the debt-creation boundary with the post-maturity liquidation boundary (`block.timestamp > market.maturity`) and eliminates the one-second window where both guards simultaneously fail to protect the protocol.

## Proof of Concept
1. Deploy a market with `maturity = T`.
2. At block timestamp `T`, call `take()` with a sell offer where the seller has collateral but no existing credit.
3. Observe: `sellerDebtIncrease > 0` passes the guard at line 391; `sellerPos.debt` is incremented; `buyerPendingFeeIncrease == 0`.
4. In the same block, attempt `liquidate(..., postMaturityMode=true)` — reverts with `NotLiquidatable()` because `block.timestamp > market.maturity` is `false`.
5. Attempt `liquidate(..., postMaturityMode=false)` — reverts with `NotLiquidatable()` because the position is healthy (collateral sufficient).
6. Confirm: newly created debt is unliquidatable for the duration of block `T`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** src/Midnight.sol (L359-359)
```text
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
```

**File:** src/Midnight.sol (L383-384)
```text
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
```

**File:** src/Midnight.sol (L385-386)
```text
        uint128 buyerPendingFeeIncrease =
            UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
```

**File:** src/Midnight.sol (L391-391)
```text
        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

**File:** src/Midnight.sol (L414-414)
```text
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/Midnight.sol (L476-476)
```text
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```
