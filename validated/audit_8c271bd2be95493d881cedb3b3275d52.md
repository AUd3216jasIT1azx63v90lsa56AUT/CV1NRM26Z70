Audit Report

## Title
Debt Increase Permitted at Exact Maturity Boundary via `<=` Operator in `CannotIncreaseDebtPostMaturity` Check - (File: src/Midnight.sol)

## Summary
The guard at `src/Midnight.sol` line 391 uses `block.timestamp <= offer.market.maturity`, which evaluates to `true` when `block.timestamp == maturity`, allowing `sellerDebtIncrease > 0` to pass. This is inconsistent with the post-maturity liquidation check at line 622, which uses the strict `block.timestamp > market.maturity`, creating a one-block window where a borrower can increase debt at exactly maturity while the position is immune to post-maturity liquidation.

## Finding Description
**`UtilsLib.zeroFloorSub` (src/libraries/UtilsLib.sol line 24):**
```solidity
z := mul(gt(x, y), sub(x, y))
```
When `x == y`, `gt(x,y) = 0`, so `timeToMaturity = 0` at `block.timestamp == maturity`.

**Guard at src/Midnight.sol line 391:**
```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
At `block.timestamp == maturity`, the left side is `true`, so the `require` passes unconditionally regardless of `sellerDebtIncrease`. The debt increase at line 414 (`sellerPos.debt += sellerDebtIncrease`) executes.

**Post-maturity liquidation guard at src/Midnight.sol line 622:**
```solidity
postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt
```
Uses strict `>`, so at `block.timestamp == maturity`, post-maturity liquidation is unavailable. Normal-mode liquidation requires the position to be unhealthy. A healthy borrower who increases debt at exactly maturity is immune to both liquidation paths for that block.

**Downstream at `timeToMaturity = 0`:**
- `buyerPendingFeeIncrease = buyerCreditIncrease.mulDivDown(continuousFee * 0, WAD) = 0` (lines 385–386): new lender credit carries zero continuous-fee obligation.
- `settlementFee(id, 0)` falls into the `timeToMaturity < 1 days` branch (line 971), returning the 0-day breakpoint fee — internally consistent but the trade itself should not have been permitted.

## Impact Explanation
The protocol invariant that debt increase must be forbidden at or after maturity is violated. A borrower can open or increase a debt position at the exact maturity timestamp — a point at which the position is immediately overdue — while post-maturity liquidation is blocked for that block. The new lender credit accrues zero continuous fee. Although the position must pass the health check (line 476), the invariant breach is concrete and reproducible, and the one-block liquidation immunity is a real protocol state inconsistency.

## Likelihood Explanation
The precondition is a single specific `block.timestamp == maturity`. Any borrower or MEV searcher who controls transaction submission can target this block deterministically. The offer need only have a non-expired `expiry` covering that timestamp and sufficient collateral pre-supplied. No privileged access is required. The condition is repeatable for every market whose maturity falls on a reachable block timestamp.

## Recommendation
Change the guard at `src/Midnight.sol` line 391 from `<=` to `<`:
```solidity
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
This makes the debt-increase boundary consistent with the post-maturity liquidation check (`block.timestamp > market.maturity`), closing the one-block window at exact maturity.

## Proof of Concept
1. Deploy a market with `maturity = T`.
2. Pre-supply collateral so the resulting position will be healthy.
3. Create a sell offer (`offer.buy = false`, `offer.maker = borrower`) with `expiry >= T`.
4. Warp to `block.timestamp == T`.
5. Call `take` with `units > sellerPos.credit` so `sellerDebtIncrease > 0`.
6. Observe line 391 passes (`T <= T` is `true`), `sellerPos.debt` increases at line 414.
7. Confirm post-maturity liquidation reverts (`block.timestamp > market.maturity` is `false`).
8. Confirm normal liquidation reverts if position is healthy.
9. Advance one block; post-maturity liquidation now succeeds — demonstrating the one-block immunity window. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** src/Midnight.sol (L359-359)
```text
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
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

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/Midnight.sol (L971-971)
```text
            timeToMaturity < 1 days   ? (  0 days,   1 days, _marketState.settlementFeeCbp0 * CBP, _marketState.settlementFeeCbp1 * CBP) :
```

**File:** src/libraries/UtilsLib.sol (L22-26)
```text
    function zeroFloorSub(uint256 x, uint256 y) internal pure returns (uint256 z) {
        assembly {
            z := mul(gt(x, y), sub(x, y))
        }
    }
```
