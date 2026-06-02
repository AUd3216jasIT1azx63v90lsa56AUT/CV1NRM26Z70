Audit Report

## Title
Rounding-to-zero `buyerAssets` bypasses `maxAssets` cap on fully-consumed buy offers, enabling unbounded credit inflation — (`src/Midnight.sol`)

## Summary
When a buy offer uses `maxAssets` as its cap and `buyerPrice < WAD`, calling `take` with `units = 1` produces `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`. Because `consumed` is incremented by `buyerAssets` (not `units`), the cap check `newConsumed <= maxAssets` always passes even after the offer is fully consumed. Each such call increases the maker's credit and the taker's debt by 1 unit with zero asset transfer, permanently violating the invariant that every credit unit has a matching asset payment.

## Finding Description

**Root cause — `src/Midnight.sol` lines 363–369:** [1](#0-0) 

`buyerAssets` is computed with `mulDivDown`, which rounds toward zero. When `units = 1` and `buyerPrice < WAD`, the result is `0`. The consumed accounting for assets-capped buy offers then adds `0` to `consumed[maker][group]`, so the check `newConsumed <= maxAssets` becomes `maxAssets + 0 <= maxAssets` — permanently satisfied regardless of how many times it is called.

Position accounting at lines 382–417 still executes with `units = 1`: [2](#0-1) 

- `buyerCreditIncrease = zeroFloorSub(1, buyerPos.debt)` → maker credit +1
- `sellerDebtIncrease = 1 - sellerCreditDecrease` → taker debt +1
- `totalUnits` increases by `buyerCreditIncrease - sellerCreditDecrease`
- `claimableSettlementFee += buyerAssets - sellerAssets = 0`

No tokens are transferred, but position state mutates on every call.

**Certora spec gap — `certora/specs/Consume.spec` lines 88–97:** [3](#0-2) 

`takeConsumedAtMaxUnchangedAssets` only asserts that `consumed` does not change — which is true when `buyerAssets = 0`. It does not assert that credit, debt, or `totalUnits` are also unchanged. Additionally, the spec uses `NONDET` summaries for `mulDivDown` and `mulDivUp`: [4](#0-3) 

This means the prover never models the actual rounding behavior, so it cannot detect the zero-rounding case.

**Protocol acknowledgment — `src/Midnight.sol` line 94:** [5](#0-4) 

The NatSpec explicitly documents this edge case. The test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` confirms the exact state change is reproducible.

## Impact Explanation
A fully-consumed assets-capped buy offer can be re-filled an unlimited number of times at zero asset cost. Each call mints 1 credit unit to the maker and 1 debt unit to the taker without any token transfer. This breaks the core accounting invariant that every credit unit corresponds to an asset payment, inflates `totalUnits` beyond the intended cap, and allows a maker (via a colluding taker) to accumulate unbounded credit beyond their `maxAssets` budget.

## Likelihood Explanation
**Preconditions:** (1) A buy offer exists with `maxAssets > 0` and a tick such that `buyerPrice < WAD` — a normal, reachable tick range. (2) The offer has been fully consumed through normal fills. Both conditions are reachable without any privileged access. Any unprivileged taker can trigger the bypass with a single `take` call. The taker incurs 1 unit of debt per call with no asset receipt (self-harmful), but a griefing attacker or a smart contract indifferent to its own debt can repeat this indefinitely. The bypass condition is permanent once the offer is fully consumed, since `consumed` never increases past `maxAssets`.

## Recommendation
Add a guard before the consumed accounting that rejects non-zero `units` when `buyerAssets` rounds to zero in assets-cap mode:

```solidity
// In the maxAssets branch, for buy offers:
require(units == 0 || buyerAssets > 0, ZeroAssets());
```

Alternatively, track consumed in `units` for buy offers as well and convert `maxAssets` to a units-equivalent cap at offer creation, eliminating the rounding discrepancy entirely. The Certora spec rule `takeConsumedAtMaxUnchangedAssets` should also be strengthened to assert that position state (credit, debt, `totalUnits`) is unchanged when `consumedBefore >= offer.maxAssets`.

## Proof of Concept
The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` is a direct PoC: [6](#0-5) 

Manual reproduction steps:
1. Create a buy offer with `maxAssets = N` and `tick = MAX_TICK - 16` (so `buyerPrice < WAD`).
2. Fill the offer to `consumed[maker][group] == N` (fully consumed).
3. Call `take(offer, ..., units=1, ...)` from an unprivileged taker address.
4. Observe: `consumed` unchanged at `N`, maker credit +1, taker debt +1, `totalUnits` +1, zero tokens transferred.
5. Repeat step 3 indefinitely — each call succeeds.

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

**File:** src/Midnight.sol (L382-418)
```text
        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
        uint128 buyerPendingFeeIncrease =
            UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
        uint128 sellerPendingFeeDecrease = sellerPos.credit > 0
            ? UtilsLib.toUint128(sellerPos.pendingFee.mulDivUp(sellerCreditDecrease, sellerPos.credit))
            : 0;

        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
        require(
            !offer.reduceOnly || (offer.buy ? buyerCreditIncrease == 0 : sellerDebtIncrease == 0),
            MakerCreditOrDebtIncreased()
        );

        require(
            offer.market.enterGate == address(0) || buyerCreditIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseCredit(buyer),
            BuyerGatedFromIncreasingCredit()
        );
        require(
            offer.market.enterGate == address(0) || sellerDebtIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseDebt(seller),
            SellerGatedFromIncreasingDebt()
        );

        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);

        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

**File:** certora/specs/Consume.spec (L11-12)
```text
    function UtilsLib.mulDivDown(uint256, uint256, uint256) internal returns (uint256) => NONDET;
    function UtilsLib.mulDivUp(uint256, uint256, uint256) internal returns (uint256) => NONDET;
```

**File:** certora/specs/Consume.spec (L88-97)
```text
/// If consumed is already at or above maxAssets before a take in assets mode, it remains unchanged.
rule takeConsumedAtMaxUnchangedAssets(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxUnits == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert consumedBefore >= offer.maxAssets => consumed(offer.maker, offer.group) == consumedBefore;
}
```

**File:** test/TakeTest.sol (L1-1)
```text
// SPDX-License-Identifier: GPL-2.0-or-later
```
