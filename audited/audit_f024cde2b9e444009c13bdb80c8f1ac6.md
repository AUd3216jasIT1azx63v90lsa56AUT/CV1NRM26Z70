Audit Report

## Title
Fully-consumed assets-mode buy offer can be re-taken with `units > 0` when `buyerPrice < WAD`, minting unbacked credit and debt - (`File: src/Midnight.sol`, `src/libraries/TickLib.sol`)

## Summary
In `Midnight.take`, when a buy offer uses `maxAssets` mode, the consumed-assets delta is computed as `mulDivDown(units, buyerPrice, WAD)`. Because `tickToPrice` returns a value strictly less than `WAD` for every valid tick, this expression evaluates to `0` for `units = 1`. A fully-consumed offer therefore passes the `require(newConsumed <= offer.maxAssets)` guard indefinitely while still executing the full position-accounting path, minting one unit of unbacked credit to the maker and one unit of debt to the taker per call with zero token transfers.

## Finding Description

**Root cause — `src/Midnight.sol` lines 363–373:**

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // rounds DOWN → 0 when units=1
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

**Why `buyerPrice < WAD` always holds — `src/libraries/TickLib.sol` lines 44–52:**

```solidity
return uint256(1e36)
    .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
    .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
```

The denominator `1e18 + wExp(...)` is always strictly greater than `1e18`, so the result is always strictly less than `1e18 = WAD` for all 5820 valid ticks. Therefore `mulDivDown(1, buyerPrice, WAD) = floor(buyerPrice / WAD) = 0` for every valid tick.

**Exploit flow:**
1. A buy offer exists with `offer.buy = true`, `offer.maxAssets = M > 0`, any valid tick.
2. `consumed[maker][group]` reaches `M` through normal fills.
3. Attacker calls `take(units=1, ...)`:
   - `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`
   - `newConsumed = M + 0 = M`; `require(M <= M)` passes
   - `buyerCreditIncrease = zeroFloorSub(1, buyerPos.debt) = 1` (if maker has no debt)
   - `sellerDebtIncrease = 1 - 0 = 1` (if taker has no credit)
   - `buyerPos.credit += 1` — maker receives 1 unit of credit, no loan tokens deposited
   - `sellerPos.debt += 1` — taker receives 1 unit of debt, no loan tokens received
   - `totalUnits += 1`
   - No token transfers occur (`buyerAssets = sellerAssets = 0`)
4. Step 3 repeats indefinitely; `consumed` stays pinned at `M` forever.

**Why existing checks fail:**

The sole guard is `require(newConsumed <= offer.maxAssets)`. Since `buyerAssets = 0`, `newConsumed` never advances, making the check permanently bypassable. There is no guard of the form `require(units == 0 || buyerAssets > 0)`.

The Certora rule `takeConsumedAtMaxUnchangedAssets` (`certora/specs/Consume.spec` lines 88–97) only asserts that `consumed` is unchanged when already at max — which is trivially satisfied since `buyerAssets = 0` — but does not assert `units == 0`. The rule `fullyConsumedOfferRevertsOnNonTrivialTake` (lines 99–111) only covers `maxAssets == 0` (units mode); no equivalent rule exists for assets mode. The protocol's own NatDoc at `src/Midnight.sol` line 94 explicitly acknowledges: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* The test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` confirms this is a reachable, known state.

## Impact Explanation

Each successful re-take with `units = 1` on a fully-consumed buy offer mints 1 unit of credit to the maker without any corresponding loan-token deposit, and 1 unit of debt to the taker without any loan-token transfer. It also increments `totalUnits`, inflating the continuous-fee accrual base for all lenders. The contract's loan-token balance no longer covers all outstanding credit, violating the core protocol invariant that contract balances cover credit redemption and withdrawable assets. The `maxAssets` cap — intended to bound the maker's total exposure — is rendered permanently ineffective. This constitutes direct protocol insolvency through unbounded unbacked credit/debt minting, triggerable by any unprivileged user.

## Likelihood Explanation

**Preconditions:**
- A buy offer with `maxAssets > 0` at any valid tick (structural: holds for all 5820 ticks).
- `consumed[maker][group] == maxAssets` (normal end state after full consumption).
- Attacker is any unprivileged address that is not the maker (only the `SelfTake` guard applies).

**Feasibility:** High. The `buyerPrice < WAD` condition is structural and permanent — no oracle manipulation, no admin action, no special token behavior required. The attacker only needs to call `take` with `units = 1` after any offer is exhausted.

**Repeatability:** Unlimited. Each call succeeds and adds 1 unit of unbacked credit/debt. The consumed counter stays pinned at `maxAssets` indefinitely.

## Recommendation

Add a guard in the `maxAssets` branch that rejects non-zero `units` when the computed asset delta is zero:

```solidity
if (offer.maxAssets > 0) {
    uint256 assetsDelta = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || assetsDelta > 0, ZeroAssetDelta());
    newConsumed = consumed[offer.maker][offer.group] += assetsDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, add a corresponding Certora rule analogous to `fullyConsumedOfferRevertsOnNonTrivialTake` for the assets mode: assert that when `maxAssets > 0` and `consumedBefore >= maxAssets`, a successful `take` must have `units == 0`.

## Proof of Concept

**Minimal Foundry test outline:**

```solidity
function testBugBuyMaxAssetsBypass() public {
    // 1. Create a buy offer with maxAssets = M at any valid tick (e.g. tick=0)
    // 2. Fill the offer normally until consumed[maker][group] == M
    // 3. Record maker.credit and taker.debt before attack
    // 4. Call take(units=1, taker=attacker, offer=buyOffer)
    // 5. Assert: take did NOT revert
    // 6. Assert: maker.credit increased by 1 with no loan token deposited
    // 7. Assert: attacker.debt increased by 1 with no loan token received
    // 8. Assert: consumed[maker][group] == M (unchanged)
    // 9. Repeat step 4 N times; assert maker.credit == N, totalUnits increased by N
    // 10. Assert: loanToken.balanceOf(contract) < sum of all outstanding credit
}
```

The test `testBugBuyMaxAssetsBypass` already exists in `test/TakeTest.sol` and can be run directly to confirm the behavior.