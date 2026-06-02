Audit Report

## Title
`maxAssets` cap permanently bypassed via zero-rounding of `buyerAssets` on buy offers with `offerPrice < WAD` - (File: `src/Midnight.sol`)

## Summary
When a buy offer has `maxAssets > 0` and `offerPrice < WAD` (tick > `MAX_TICK/2`), calling `take()` with `units = 1` computes `buyerAssets = mulDivDown(1, offerPrice, WAD) = 0`. Because `consumed` is incremented by `buyerAssets` (not `units`), the cap check at line 369 is a permanent no-op, allowing the offer to be taken an unlimited number of times. An unprivileged taker can exploit this to accumulate unbounded debt at zero token cost, then default at maturity to force bad-debt socialization onto all lenders in the market.

## Finding Description

**Exact code path** — `src/Midnight.sol`, `take()`:

```solidity
// Line 363
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // = mulDivDown(1, offerPrice, WAD) = 0
    : units.mulDivUp(buyerPrice, WAD);

// Lines 367-369
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    //                                                              ^^^^^^^^^^^ += 0
    require(newConsumed <= offer.maxAssets, ConsumedAssets());  // always passes
}
```

`buyerPrice` equals `offerPrice` when `_settlementFee = 0`, and for any tick > 2910 (`offerPrice < 1e18`), `mulDivDown(1, offerPrice, 1e18) = 0`. The `consumed` mapping never advances, so the cap is never enforced.

**Why existing checks fail:** The sole guard is `require(newConsumed <= offer.maxAssets)` at line 369. Since `newConsumed` is unchanged on every iteration, this check trivially passes indefinitely regardless of how many times the offer has been taken.

**Downstream consequence:** With `buyerAssets = sellerAssets = 0`, the token transfers at lines 455–456 move zero tokens. The taker's debt is nonetheless incremented by 1 unit per call (line 414: `sellerPos.debt += sellerDebtIncrease`). The taker accumulates unbounded on-chain debt with zero loan-token payment. At maturity, this unpaid debt is realized as bad debt and socialized across all lenders via the slashing mechanism described in the contract's NatSpec.

**Protocol acknowledgment:** The protocol's own NatSpec at line 94 states this is reachable, and the test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` confirms it is a recognized bug — not an intended design choice.

## Impact Explanation

An attacker (taker) can:
1. Loop `take(units=1)` against any qualifying buy offer at zero token cost.
2. Accumulate arbitrarily large debt positions with no corresponding loan-token receipt.
3. Default at maturity, forcing the entire bad-debt amount to be socialized among all lenders in the market via proportional credit slashing.

This constitutes direct, unbounded theft from lenders — a critical accounting integrity failure. The `maxAssets` cap, the protocol's primary mechanism for limiting offer exposure, is rendered completely ineffective for the affected tick range.

## Likelihood Explanation

**Preconditions:**
- A buy offer exists with `maxAssets > 0` and any tick in `(MAX_TICK/2, MAX_TICK]` (i.e., `offerPrice < WAD`). This is a normal, valid configuration covering half of all valid ticks.
- The attacker is any unprivileged taker — no special role, no admin access, no oracle manipulation required.
- The offer must not be expired and must pass ratifier checks (standard conditions).

**Feasibility:** Fully reachable on-chain. The loop cost is gas only. Each iteration is independent and leaves state ready for the next call. The condition is permanent until the offer expires or is cancelled.

## Recommendation

Replace the `buyerAssets`-based consumed increment with a check that prevents zero-asset takes from bypassing the cap. Two viable approaches:

1. **Revert on zero-asset take when `maxAssets > 0`:** Add `require(buyerAssets > 0 || offer.maxAssets == 0)` before the consumed block. This prevents the degenerate case entirely.

2. **Fall back to unit-based tracking when `buyerAssets = 0`:** When `offer.maxAssets > 0` and `buyerAssets = 0`, increment `consumed` by `units` instead and compare against a separate `maxUnits`-equivalent derived from `maxAssets`. This preserves offer usability while enforcing a meaningful cap.

Option 1 is simpler and eliminates the attack surface with minimal code change.

## Proof of Concept

Minimal reproduction (matches the existing `testBugBuyMaxAssetsBypass` pattern in `test/TakeTest.sol`):

1. Deploy market with `tickSpacing` dividing `MAX_TICK - 16 = 5804`.
2. Maker posts buy offer: `tick = 5804`, `maxAssets = 1000e18`, `maxUnits = 0`.
3. Verify `tickToPrice(5804) < WAD` → `offerPrice < 1e18`.
4. Taker calls `take(units=1)` in a loop 10,000 times.
5. Assert: `consumed[maker][group]` remains 0 after all iterations.
6. Assert: taker's debt = 10,000 units; tokens transferred = 0.
7. Assert: all 10,000 calls succeed despite `maxAssets = 1000e18` being nominally a binding cap. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** src/Midnight.sol (L94-94)
```text
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L363-369)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L408-414)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```
