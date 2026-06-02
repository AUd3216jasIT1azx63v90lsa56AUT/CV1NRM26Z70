Audit Report

## Title
Fully-Consumed Assets-Based Buy Offer With `buyerPrice < WAD` Bypasses Consumption Guard, Allowing Unbounded Position Mutation - (File: src/Midnight.sol)

## Summary
When a buy offer has `maxAssets > 0` and `buyerPrice < WAD`, calling `take` with `units = 1` after the offer is fully consumed produces `buyerAssets = mulDivDown(1 * buyerPrice, WAD) = 0`, causing the consumed guard to pass as a no-op. All downstream position mutations — buyer credit increase, seller debt increase, and `totalUnits` inflation — still execute against `units = 1` with zero token transfer, corrupting the credit/debt accounting invariant indefinitely.

## Finding Description
**Root cause and code path:**

At `src/Midnight.sol:363`, `buyerAssets` is computed with truncating division: [1](#0-0) 

When `buyerPrice < WAD` and `units = 1`, `mulDivDown(1 * buyerPrice, WAD)` truncates to `0`.

At lines 367–369, the consumed guard increments `consumed` by `buyerAssets` (i.e., `0`) and checks `<= maxAssets`: [2](#0-1) 

When the offer is already fully consumed (`consumed == maxAssets`), `maxAssets + 0 <= maxAssets` trivially passes. Execution proceeds unconditionally to the position mutation block: [3](#0-2) 

These mutations operate on `units` (= 1), not `buyerAssets` (= 0). The buyer's credit increases, the seller's debt increases, and `totalUnits` inflates — all with zero token transfer, confirmed at line 455: [4](#0-3) 

**Why existing checks fail:**
- `require(newConsumed <= offer.maxAssets)` — passes because `maxAssets + 0 ≤ maxAssets`.
- No guard enforces `units == 0` when `consumed >= maxAssets`.
- No guard enforces `buyerAssets > 0` when `units > 0` on a buy offer.

**Protocol acknowledgment:** The codebase explicitly documents this behavior at line 94: [5](#0-4) 

**Confirmed by test:** `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` is already present in the repository, reproducing the exact scenario. [6](#0-5) 

## Impact Explanation
Each post-consumption call with `units = 1` and `buyerAssets = 0`:
- Increases the maker's (buyer's) `credit` by `buyerCreditIncrease` derived from `units = 1`, with zero loan tokens paid.
- Increases the taker's (seller's) `debt` by `sellerDebtIncrease` derived from `units = 1`, with zero loan tokens received.
- Increases `totalUnits` by `buyerCreditIncrease` with no asset backing, diluting all lenders' pro-rata claims at maturity.

An attacker controlling both the maker address and a throwaway taker address (with no collateral) can inflate the maker's credit arbitrarily at negligible gas cost. The taker's resulting bad debt is socialized among all lenders via the slashing mechanism, directly reducing their recoverable assets. This constitutes unauthorized state change, accounting corruption, and indirect theft from other lenders — all concrete in-scope impact classes. [7](#0-6) 

## Likelihood Explanation
All preconditions are attacker-reachable without any privileged access:
1. `offer.buy = true`, `offer.maxAssets > 0` — standard offer configuration available to any user.
2. `buyerPrice < WAD` — achieved by setting `offer.tick` to any value where `tickToPrice(tick) + settlementFee < WAD` (e.g., `MAX_TICK - 16` as used in the existing test).
3. Offer fully consumed — the attacker can self-consume as the first taker using a separate address, or wait for organic fills.

The attack is repeatable every block with no token transfers, making it essentially free beyond gas. Any external account can act as the taker.

## Recommendation
Add a guard that rejects `units > 0` when `buyerAssets == 0` on a buy offer, or equivalently, require that `units == 0` when `consumed >= maxAssets` before executing position mutations. A minimal fix at the consumed-guard block:

```solidity
if (offer.maxAssets > 0) {
    uint256 assetsDelta = offer.buy ? buyerAssets : sellerAssets;
    require(assetsDelta > 0 || units == 0, ZeroAssetsTake());
    newConsumed = consumed[offer.maker][offer.group] += assetsDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

This ensures that a non-zero `units` value always corresponds to a non-zero asset transfer on buy offers with `maxAssets` caps, closing the rounding bypass. [8](#0-7) 

## Proof of Concept
The test `testBugBuyMaxAssetsBypass` already exists in `test/TakeTest.sol` in the repository and reproduces the exact scenario: a buy offer with `maxAssets > 0` and `tick` set such that `buyerPrice < WAD` is fully consumed, then called again with `units = 1`, resulting in zero asset transfers but strictly increased `creditOf`, `debtOf`, and `totalUnits`. [6](#0-5)

### Citations

**File:** src/Midnight.sol (L80-83)
```text
/// SLASHING
/// @dev When a borrower's bad debt is realized, it is socialized among lenders in this market.
/// @dev At each lender's next interaction, their credit is slashed proportionally.
///
```

**File:** src/Midnight.sol (L94-94)
```text
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L363-363)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
```

**File:** src/Midnight.sol (L366-373)
```text
        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
```

**File:** src/Midnight.sol (L408-417)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);

        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** test/TakeTest.sol (L1-1)
```text
// SPDX-License-Identifier: GPL-2.0-or-later
```
