All the code references check out. Let me verify the `sellerPrice`/`buyerPrice` algebra and the Certora spec gap before rendering a verdict.

Audit Report

## Title
Fully-consumed buy offer with `buyerPrice < WAD` allows unlimited zero-cost re-takes via rounding bypass - (File: `src/Midnight.sol`)

## Summary
When a buy offer uses assets-mode (`maxAssets > 0`) and `tickToPrice(offer.tick) < WAD` (any tick below MAX_TICK = 5820), calling `take` with `units` small enough that `units * buyerPrice < WAD` produces `buyerAssets = 0` via integer truncation. If the offer is already fully consumed (`consumed == maxAssets`), the zero increment leaves `newConsumed == maxAssets`, the cap check passes, and the take executes — modifying buyer credit and seller debt by `units` — with zero token payment. This is repeatable indefinitely.

## Finding Description

**Root cause — integer truncation to zero:**

`buyerAssets` is computed at line 363 as:

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
```

`mulDivDown` in `src/libraries/UtilsLib.sol` line 30 is `(x * y) / d`. When `units * buyerPrice < WAD`, the result truncates to 0.

For a buy offer, `buyerPrice` resolves to `offerPrice` exactly:
- `sellerPrice = offerPrice - _settlementFee` (line 361)
- `buyerPrice = sellerPrice + _settlementFee = offerPrice` (line 362)

So `buyerPrice = tickToPrice(offer.tick)`, which is strictly less than `WAD` for any tick below MAX_TICK (5820). With `units = 1`, `1 * buyerPrice < WAD` holds for all such ticks, yielding `buyerAssets = 0`.

**Consumed cap bypass:**

Lines 367–369 increment `consumed` by `buyerAssets`:

```solidity
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

When `buyerAssets = 0`, `consumed` is unchanged. If `consumed == maxAssets` already, then `newConsumed = maxAssets` and `require(maxAssets <= maxAssets)` trivially passes.

**Position state modified from `units`, not `buyerAssets`:**

Lines 382–414 compute `buyerCreditIncrease`, `sellerDebtIncrease`, etc. directly from `units`. With `units = 1`, `buyerPos.credit += 1` and `sellerPos.debt += 1` execute unconditionally regardless of `buyerAssets`.

**Token transfers are zero:**

Lines 455–456 transfer `buyerAssets - sellerAssets = 0` and `sellerAssets = 0`, both valid ERC-20 zero-value transfers that succeed silently.

**Certora spec gap:**

`fullyConsumedOfferRevertsOnNonTrivialTake` (lines 99–111 of `Consume.spec`) requires `offer.maxAssets == 0` (line 101), so it only covers units mode — there is no equivalent rule for assets mode. `takeConsumedAtMaxUnchangedAssets` (lines 88–97) only asserts that `consumed` does not change when `consumedBefore >= maxAssets`, which is vacuously satisfied when `buyerAssets = 0`, but does not assert that `units == 0` or that the take reverts.

## Impact Explanation
A fully-consumed buy offer with any tick below MAX_TICK (5820) can be taken an unlimited number of times with `units > 0` and zero token cost. Each call increases the buyer's credit and the seller's debt by `units` without any corresponding loan token payment. This allows a taker to inflate credit/debt positions for free, bypassing the offer's intended fill cap entirely. The invariant that offers cannot be overfilled or replayed after exhaustion is violated, enabling unbounded artificial debt creation against any seller who has posted such an offer.

## Likelihood Explanation
The precondition `buyerPrice < WAD` is satisfied by any tick below MAX_TICK (5820), covering the vast majority of all valid ticks. No special permissions are required — any unprivileged taker can call `take` with `units = 1`. The offer does not need to be maliciously crafted; any legitimate buy offer with a sub-par price is vulnerable once `consumed` reaches `maxAssets`. The exploit is repeatable within a single transaction or across blocks at negligible gas cost.

## Recommendation
Add a guard that rejects takes where `units > 0` but the computed asset amount is zero in assets mode. Concretely, after computing `buyerAssets`/`sellerAssets`, require:

```solidity
if (offer.maxAssets > 0) {
    require(units == 0 || (offer.buy ? buyerAssets : sellerAssets) > 0, ZeroAssetTake());
}
```

Alternatively, enforce a minimum `units` such that the asset amount is always non-zero, or switch the consumed tracking to always use `units` (not assets) so the cap is never bypassable via rounding. The Certora spec should also add an assets-mode analogue of `fullyConsumedOfferRevertsOnNonTrivialTake` that asserts a fully-consumed assets-mode offer cannot be taken with `units > 0`.

## Proof of Concept

1. Deploy the protocol and create a market with `tickSpacing = 1`.
2. Lender posts a buy offer with `tick = 2910` (midpoint, `tickToPrice(2910) ≈ 0.5e18 < WAD`), `maxAssets = 1e6`, `buy = true`.
3. Taker calls `take` with `units = 1` repeatedly until `consumed[maker][group] == maxAssets` (legitimate fills).
4. At this point the offer is fully consumed. Taker calls `take` again with `units = 1`:
   - `buyerPrice = tickToPrice(2910) ≈ 0.5e18`
   - `buyerAssets = mulDivDown(1, 0.5e18, 1e18) = 0`
   - `newConsumed = maxAssets + 0 = maxAssets` → cap check passes
   - `buyerPos.credit += 1`, `sellerPos.debt += 1`
   - Token transfer: 0
5. Repeat step 4 indefinitely. Each iteration costs only gas and inflates the seller's debt by 1 unit with no payment. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/Midnight.sol (L361-369)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L382-414)
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
```

**File:** src/libraries/UtilsLib.sol (L28-31)
```text
    /// @dev Returns (x * y) / d rounded down.
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** src/libraries/TickLib.sol (L6-6)
```text
uint256 constant MAX_TICK = 5820;
```

**File:** certora/specs/Consume.spec (L88-111)
```text
/// If consumed is already at or above maxAssets before a take in assets mode, it remains unchanged.
rule takeConsumedAtMaxUnchangedAssets(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxUnits == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert consumedBefore >= offer.maxAssets => consumed(offer.maker, offer.group) == consumedBefore;
}

/// A fully-consumed offer in units mode only allows no-op takes.
rule fullyConsumedOfferRevertsOnNonTrivialTake(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxAssets == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    require offer.maxUnits > 0 && consumedBefore >= offer.maxUnits, "assume the offer is fully consumed";

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    // If take does not revert, its input has to be zero.
    assert units == 0;
}
```
