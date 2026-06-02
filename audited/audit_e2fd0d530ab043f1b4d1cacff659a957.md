Audit Report

## Title
Missing minimum maturity check allows creation of permanently unusable markets - (File: src/Midnight.sol)

## Summary
`touchMarket` enforces only an upper bound on `market.maturity` but no lower bound, allowing any unprivileged caller to create a market with `maturity < block.timestamp`. Because `take()` unconditionally reverts with `CannotIncreaseDebtPostMaturity` when `block.timestamp > market.maturity` and `sellerDebtIncrease > 0`, and every take in a fresh market produces `sellerDebtIncrease = units > 0`, the market is permanently locked from its first block of existence.

## Finding Description
**Root cause — `touchMarket`, `src/Midnight.sol:758`:**

```solidity
require(market.maturity <= block.timestamp + 100 * 365 days, MaturityTooFar());
// No lower bound: market.maturity = block.timestamp - 1 passes this check
```

The only maturity guard is an upper bound. Passing `maturity = block.timestamp - 1` satisfies it, and the market is created with `tickSpacing = DEFAULT_TICK_SPACING` at line 776, marking it as a valid, initialized market.

**Trigger — `take()`, `src/Midnight.sol:359,384,391`:**

```solidity
uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp); // = 0
uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);                   // = 0 (fresh market)
uint256 sellerDebtIncrease = units - sellerCreditDecrease;                               // = units
...
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
// false || (units == 0) → reverts for any units > 0
```

In a freshly created market, every position has `credit = 0`, so `sellerCreditDecrease = 0` and `sellerDebtIncrease = units`. With `block.timestamp > market.maturity`, the require reverts for any non-zero `units`, on both buy and sell offers.

**Existing protections are insufficient:**
- `MaturityTooFar` (line 758) only blocks maturities more than 100 years in the future — it does not prevent past maturities.
- No other guard in `touchMarket` checks that maturity is in the future.
- The `CannotIncreaseDebtPostMaturity` check in `take()` is a consequence of the broken state, not a protection against it.

## Impact Explanation
A market is stored on-chain with `tickSpacing > 0` (i.e., recognized as created), yet its core function — matching lenders and borrowers via `take()` — is permanently and irrecoverably broken from block 0. Operations like `supplyCollateral` (which calls `touchMarket` and succeeds) can deposit funds into a market where no borrower can ever be matched. This constitutes a permanent freeze of market state and potential lock of user funds, which is an in-scope impact per RESEARCHER.md ("Permanent lock, freeze, or unrecoverable corruption of user/project state").

## Likelihood Explanation
The precondition is trivially achievable: any EOA can call `touchMarket` with `maturity = block.timestamp - 1` and any otherwise-valid market parameters (sorted collaterals, allowed LLTV, correct `maxLif`). No privilege, capital, oracle manipulation, or victim cooperation is required. The call costs only gas and is repeatable for any unique combination of market parameters not yet initialized.

## Recommendation
Add a minimum maturity check in `touchMarket` immediately after (or alongside) the existing upper-bound check:

```solidity
require(market.maturity > block.timestamp, MaturityInThePast());
require(market.maturity <= block.timestamp + 100 * 365 days, MaturityTooFar());
```

This ensures every newly created market has a positive `timeToMaturity` at creation, making `take()` operable from the start.

## Proof of Concept
1. Deploy or fork the contract.
2. Construct a valid `Market` struct with `maturity = block.timestamp - 1`, valid sorted collaterals, allowed LLTV, and correct `maxLif`.
3. Call `midnight.touchMarket(market)` — succeeds, market is created (emits `MarketCreated`, `marketState[id].tickSpacing == DEFAULT_TICK_SPACING`).
4. Construct any `Offer` referencing this market with `units > 0`.
5. Call `midnight.take(offer, ...)` — reverts with `CannotIncreaseDebtPostMaturity`.
6. Confirm no path exists to make the market operable: `sellerPos.credit` is permanently 0 for all new positions, so `sellerDebtIncrease` is always `units > 0`, and the revert condition is permanent.