Audit Report

## Title
Buy-offer `maxAssets` cap bypassed via zero-rounding of `buyerAssets` when `offerPrice < WAD` - (File: src/Midnight.sol)

## Summary
When `offer.buy = true` and `offerPrice < WAD`, `buyerAssets` is computed via `mulDivDown` at line 363, which floors to zero for any `units` satisfying `units * buyerPrice < WAD`. Because the `consumed` accumulator at line 368 is incremented by `buyerAssets` (not `units`), a taker can call `take` with such small `units` values indefinitely without ever advancing `consumed`, bypassing the maker's `maxAssets` cap entirely. The protocol's own test suite contains `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol`, confirming this is a live defect.

## Finding Description
**Root cause — `src/Midnight.sol` lines 363–373:**

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
...
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

For a buy offer, `buyerPrice = offerPrice` (since `sellerPrice = offerPrice - _settlementFee` and `buyerPrice = sellerPrice + _settlementFee`). When `offerPrice < WAD`, any `units` satisfying `units * offerPrice < WAD` produces `buyerAssets = 0` via floor division. The `consumed` accumulator is then incremented by zero, so `newConsumed` never grows and the `ConsumedAssets` guard never fires.

**Exploit flow:**
1. Maker publishes a buy offer with `maxAssets = N > 0` and a tick where `tickToPrice(tick) < WAD`.
2. Taker calls `take(units=1, ...)` in a loop or via `multicall`.
3. Each call: `buyerAssets = 1 * offerPrice / WAD = 0`; `consumed += 0`; cap check passes.
4. Each call still executes the full position update at lines 408–414: `buyerPos.credit += buyerCreditIncrease`, `sellerPos.debt += sellerDebtIncrease`, `totalUnits` grows.
5. Token transfers at lines 455–456 move `buyerAssets - sellerAssets = 0` and `sellerAssets = 0`, so the maker accumulates credit without paying and the taker accumulates debt without receiving tokens.

**Why existing checks fail:** The `require(newConsumed <= offer.maxAssets)` guard is structurally correct but operates on `buyerAssets`. When `buyerAssets = 0`, it is a no-op. The `EcrecoverRatifier.isRatified` check only validates the Merkle proof and signature over the offer struct; it does not inspect `units` or `buyerAssets`.

## Impact Explanation
The `maxAssets` invariant — intended to bound total buyer-asset exposure per maker/group — is rendered ineffective. `totalUnits` and position credit/debt grow without bound while no loan tokens back the new credit, breaking the protocol's core accounting integrity. The maker accumulates unbacked credit claims; the taker accumulates debt with zero loan-token receipt. This constitutes a critical accounting/state integrity failure: the invariant "offers cannot be overfilled beyond `maxAssets`" is violated, and the protocol holds unbacked credit obligations.

## Likelihood Explanation
All preconditions are reachable by an unprivileged taker with no special access: (1) `offer.buy = true` — standard offer type; (2) `offer.maxAssets > 0` — standard cap usage; (3) `offerPrice < WAD` — achievable at any tick below the WAD-price tick, a normal part of the tick range; (4) `units = 1` — minimum valid input. The attack is repeatable in a single transaction via `multicall`. The protocol's own test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` confirms the path is live and passes.

## Recommendation
Increment the `consumed` accumulator by `units` (or a non-zero minimum) rather than by `buyerAssets` when `offer.maxAssets > 0`, or add a `require(buyerAssets > 0)` guard before the accumulator update. Alternatively, enforce a minimum `units` value such that `units.mulDivDown(buyerPrice, WAD) > 0` before proceeding. The most robust fix is to track consumed in `units` space uniformly and convert to assets only for the cap comparison, or to revert when the computed `buyerAssets` rounds to zero.

## Proof of Concept
The protocol's own test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` reproduces this directly. Minimal manual steps:
1. Deploy with a buy offer: `maxAssets = 1e18`, tick such that `tickToPrice(tick) = 1e17` (i.e., `offerPrice < WAD`).
2. Call `take(units=1, ...)` — `buyerAssets = 1 * 1e17 / 1e18 = 0`; `consumed` stays 0; call succeeds.
3. Repeat step 2 arbitrarily many times; `consumed` never reaches `maxAssets`; positions grow unboundedly with zero token transfer. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** src/Midnight.sol (L363-373)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
```

**File:** src/Midnight.sol (L408-418)
```text
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

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```
