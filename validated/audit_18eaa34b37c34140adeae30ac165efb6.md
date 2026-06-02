Audit Report

## Title
Debt Increase Permitted at Exact Maturity Boundary via `<=` Operator in `CannotIncreaseDebtPostMaturity` Check - (File: src/Midnight.sol)

## Summary
The guard at `src/Midnight.sol` line 391 uses `block.timestamp <= offer.market.maturity`, which evaluates to `true` when `block.timestamp == maturity`, allowing `sellerDebtIncrease > 0` to pass. This is inconsistent with the post-maturity liquidation check at line 622, which uses the strict `block.timestamp > market.maturity`, creating a one-block window where a borrower can increase debt at exactly maturity while post-maturity liquidation is simultaneously blocked. New lender credit extended at maturity accrues zero continuous fee, producing an accounting inconsistency.

## Finding Description
**Root cause — `UtilsLib.zeroFloorSub` (`src/libraries/UtilsLib.sol` line 24):**
```solidity
z := mul(gt(x, y), sub(x, y))
```
When `x == y` (i.e., `block.timestamp == maturity`), `gt(x,y) = 0`, so `timeToMaturity = 0`.

**`timeToMaturity` computation (`src/Midnight.sol` line 359):**
```solidity
uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
```
At `block.timestamp == maturity`, this returns 0.

**Guard at `src/Midnight.sol` line 391:**
```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
At `block.timestamp == maturity`, the left operand is `true`, so the `require` passes unconditionally regardless of `sellerDebtIncrease`. The debt increase at line 414 (`sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease)`) executes.

**Post-maturity liquidation guard at `src/Midnight.sol` line 622:**
```solidity
postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt
```
Uses strict `>`, so at `block.timestamp == maturity`, `postMaturityMode` liquidation reverts with `NotLiquidatable()`. Normal-mode liquidation requires `originalDebt > maxDebt` (unhealthy position). A healthy borrower who increases debt at exactly maturity is immune to both liquidation paths for that block.

**Downstream at `timeToMaturity = 0` (`src/Midnight.sol` lines 385–386):**
```solidity
uint128 buyerPendingFeeIncrease =
    UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
```
New lender credit accrues zero continuous-fee obligation, disadvantaging the lender on credit extended at maturity.

**Health check at line 476** (`require(liquidationLocked(id, seller) || isHealthy(...), SellerIsLiquidatable())`) still applies, so the position must be healthy post-trade. This does not prevent the invariant breach; it only limits the debt increase to what keeps the position healthy.

## Impact Explanation
The protocol invariant that debt increase must be forbidden at or after maturity is concretely violated. A borrower can open or increase a debt position at the exact maturity timestamp — a point at which the position is immediately overdue — while post-maturity liquidation is blocked for that block. New lender credit at maturity accrues zero continuous fee, creating an accounting inconsistency. Although the position must pass the health check, the invariant breach is reproducible and the one-block liquidation immunity is a real protocol state inconsistency.

## Likelihood Explanation
The precondition is a single specific `block.timestamp == maturity`. Any borrower or MEV searcher who controls transaction submission can target this block deterministically. The offer need only have a non-expired `expiry` covering that timestamp and sufficient collateral pre-supplied. No privileged access is required. The condition is repeatable for every market whose maturity falls on a reachable block timestamp.

## Recommendation
Change the guard at line 391 from `<=` to `<`:
```solidity
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
This aligns the debt-increase guard with the intent of the post-maturity liquidation check (strict `>` at line 622), closing the one-block window at `block.timestamp == maturity`.

## Proof of Concept
1. Deploy a market with `maturity = T`.
2. Pre-supply collateral for a borrower address such that a debt increase of `X` keeps the position healthy.
3. Submit a `take` transaction in the block where `block.timestamp == T` with `sellerDebtIncrease = X > 0`.
4. Observe: line 391 passes (left operand `true`), `sellerPos.debt` increases by `X` at line 414, `buyerPendingFeeIncrease = 0` at line 386.
5. Attempt post-maturity liquidation in the same block: line 622 evaluates `block.timestamp > market.maturity` as `false`, reverting with `NotLiquidatable()`.
6. Confirm: debt was increased at maturity, lender received zero continuous-fee accrual, and the position is unliquidatable for that block. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** src/libraries/UtilsLib.sol (L22-26)
```text
    function zeroFloorSub(uint256 x, uint256 y) internal pure returns (uint256 z) {
        assembly {
            z := mul(gt(x, y), sub(x, y))
        }
    }
```
