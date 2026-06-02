The code confirms all claims. Here is the validation:

---

Audit Report

## Title
Missing minimum maturity check allows creation of permanently unusable markets - (File: src/Midnight.sol)

## Summary
`touchMarket` enforces only an upper-bound on `market.maturity` with no lower bound, allowing any caller to create a market whose maturity is already in the past. Because every `take()` on a fresh market produces `sellerDebtIncrease = units > 0`, and the post-maturity guard unconditionally reverts when `block.timestamp > market.maturity`, the market is permanently locked from the moment of creation.

## Finding Description
**Root cause — `touchMarket`, `src/Midnight.sol:758`:**
```solidity
require(market.maturity <= block.timestamp + 100 * 365 days, MaturityTooFar());
// No lower bound: maturity = block.timestamp - 1 passes this check
```
The only maturity guard is an upper bound. Passing `maturity = block.timestamp - 1` satisfies it, and the market is initialized at line 776 with `tickSpacing = DEFAULT_TICK_SPACING`, marking it as a valid, created market.

**Trigger — `take()`, `src/Midnight.sol:383–391`:**
```solidity
uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit); // = 0 (fresh market)
uint256 sellerDebtIncrease = units - sellerCreditDecrease;            // = units
...
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
// false || (units == 0) → reverts for any units > 0
```
In a fresh market every position has `credit = 0`, so `sellerCreditDecrease = 0` and `sellerDebtIncrease = units`. With `block.timestamp > market.maturity` the require reverts for any non-zero `units` on both buy and sell offers.

**Existing protections are insufficient:**
- `MaturityTooFar` (line 758) only blocks maturities more than 100 years in the future.
- No other guard in `touchMarket` checks that maturity is in the future.
- The `CannotIncreaseDebtPostMaturity` check in `take()` is a consequence of the broken state, not a protection against it.

## Impact Explanation
A market is stored on-chain with `tickSpacing > 0` (recognized as created), yet its core function — matching lenders and borrowers via `take()` — is permanently and irrecoverably broken from block 0. Operations like `supplyCollateral` (which calls `touchMarket` and succeeds) can deposit funds into a market where no borrower can ever be matched. This constitutes a permanent freeze of market state and potential lock of user funds, which is an in-scope impact per RESEARCHER.md ("Permanent lock, freeze, or unrecoverable corruption of user/project state").

## Likelihood Explanation
The precondition is trivially achievable: any EOA can call `touchMarket` with `maturity = block.timestamp - 1` and any otherwise-valid market parameters (sorted collaterals, allowed LLTV, correct `maxLif`). No privilege, capital, oracle manipulation, or victim cooperation is required. The call costs only gas and is repeatable for any unique combination of market parameters not yet initialized.

## Recommendation
Add a minimum maturity check in `touchMarket` immediately after the existing upper-bound check:
```solidity
require(market.maturity > block.timestamp, MaturityInPast());
require(market.maturity <= block.timestamp + 100 * 365 days, MaturityTooFar());
```
This ensures that no market can be created with a maturity already elapsed, preventing the permanently-locked state.

## Proof of Concept
1. Deploy or fork the protocol locally.
2. Call `touchMarket(market)` where `market.maturity = block.timestamp - 1` and all other parameters are valid (sorted collaterals, allowed LLTV, correct `maxLif`).
3. Confirm the call succeeds and `marketState[id].tickSpacing == DEFAULT_TICK_SPACING`.
4. Attempt `take(offer, ...)` with `units > 0` against any offer in this market.
5. Observe revert with `CannotIncreaseDebtPostMaturity` — permanently, for all future blocks.
6. Optionally call `supplyCollateral` to deposit collateral into the broken market, confirming funds can be locked with no path to borrow against them.