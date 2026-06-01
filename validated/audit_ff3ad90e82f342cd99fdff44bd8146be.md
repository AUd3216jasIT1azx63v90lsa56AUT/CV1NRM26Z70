Audit Report

## Title
Zero-`buyerAssets` Take Bypasses Consumed Cap on Fully-Exhausted Assets-Based Buy Offer, Inflating Position State Without Token Transfer - (File: src/Midnight.sol)

## Summary
When a buy offer uses `maxAssets`-based accounting and `buyerPrice < WAD`, a taker can call `take()` with `units=1` on a fully consumed offer (`consumed == maxAssets`). Because `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`, the consumed mapping is incremented by zero, `newConsumed` stays at `maxAssets`, and the `require(newConsumed <= maxAssets)` guard passes trivially. The take executes in full — incrementing `buyerPos.credit`, `sellerPos.debt`, and `_marketState.totalUnits` by 1 each, and invoking the maker's callback — with zero token transfer. Since `consumed` never advances, this is repeatable without bound.

## Finding Description

**Root cause — `src/Midnight.sol` lines 363–369:**

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : ...;
// ...
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

When `offer.buy == true`, `buyerPrice < WAD` (any tick below the WAD-price threshold, e.g. `MAX_TICK - 16`), and `units = 1`, `mulDivDown(1, buyerPrice, WAD)` truncates to zero. The consumed mapping is incremented by zero, so `newConsumed` equals its pre-call value. If that value is already `maxAssets`, the `require` passes trivially.

Execution then continues to:
- `buyerPos.credit += 1` (line 410)
- `sellerPos.debt += 1` (line 414)
- `_marketState.totalUnits += buyerCreditIncrease` (line 416–417)
- `IBuyCallback.onBuy(..., buyerAssets=0, units=1, ...)` invoked (line 448–452)
- `safeTransferFrom(..., 0)` — no-op token transfers (lines 455–456)

Since `consumed` is unchanged at `maxAssets`, steps 3–8 of the exploit are repeatable indefinitely.

**Existing checks that fail to stop it:**
- `require(newConsumed <= offer.maxAssets)` — passes because the increment is zero.
- No guard that `units == 0` when `buyerAssets == 0` for assets-based offers.
- No guard that `consumed < maxAssets` before proceeding.

The protocol's own NatDoc at line 94 acknowledges this: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* The existing test `testBugBuyMaxAssetsBypass()` (lines 857–889) explicitly demonstrates and labels this as a bug, confirming the behavior is real and unfixed. [1](#0-0) [2](#0-1) [3](#0-2) 

## Impact Explanation

Each zero-cost take silently inflates `buyerPos.credit` by 1, `sellerPos.debt` by 1, and `_marketState.totalUnits` by 1 without any corresponding token transfer. This breaks the core accounting invariant that contract token balances cover credit redemption: at maturity, the inflated credit can be redeemed for tokens that were never deposited, constituting direct theft from the protocol. Additionally, the maker's `offer.callback` is invoked via `IBuyCallback.onBuy` with `buyerAssets=0` and `units=1` on an offer the maker considers fully consumed, which can corrupt any callback logic that tracks cumulative fills, releases collateral, or performs accounting under the assumption the offer is exhausted. [4](#0-3) [5](#0-4) [6](#0-5) 

## Likelihood Explanation

All preconditions are attacker-controllable or naturally occurring: `offer.buy == true` and `offer.maxAssets > 0` is a standard buy offer configuration; `buyerPrice < WAD` is satisfied by any tick below the WAD-price threshold (the test uses `MAX_TICK - 16`); `consumed == maxAssets` is the normal end-state of any legitimately filled offer; `units = 1` is trivially chosen. No privileged access, oracle manipulation, or special timing is required. The attack is repeatable in every block at negligible cost (only gas). [7](#0-6) 

## Recommendation

Add a guard immediately after computing `buyerAssets` (and `sellerAssets`) to reject a take that would result in a zero-asset increment on an assets-based offer with non-zero `units`:

```solidity
if (offer.maxAssets > 0) {
    uint256 assetsDelta = offer.buy ? buyerAssets : sellerAssets;
    require(assetsDelta > 0 || units == 0, ZeroAssetsTake());
    newConsumed = consumed[offer.maker][offer.group] += assetsDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, add a pre-check `require(consumed[offer.maker][offer.group] < offer.maxAssets)` before the increment to prevent any take on a fully consumed assets-based offer. [8](#0-7) 

## Proof of Concept

The existing test `testBugBuyMaxAssetsBypass()` in `test/TakeTest.sol` is a complete, self-contained proof of concept:

1. Creates a buy offer with `maxAssets = 1` and `tick = MAX_TICK - 16` (so `buyerPrice < WAD`).
2. Pre-sets `consumed == maxAssets` via `setConsumed`.
3. Calls `take(1, borrower, lenderOffer)`.
4. Asserts `buyerAssets == 0`, `sellerAssets == 0`, token balances unchanged.
5. Asserts `creditOf(id, lender)`, `debtOf(id, borrower)`, and `totalUnits(id)` all strictly increased — confirming position state mutation with zero token transfer. [3](#0-2)

### Citations

**File:** src/Midnight.sol (L93-94)
```text
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

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

**File:** src/Midnight.sol (L445-456)
```text
        if (buyerCallback != address(0)) {
            bytes memory buyerCallbackData = offer.buy ? offer.callbackData : takerCallbackData;
            require(
                IBuyCallback(buyerCallback)
                    .onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
                == CALLBACK_SUCCESS,
                WrongBuyCallbackReturnValue()
            );
        }

        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** test/TakeTest.sol (L857-889)
```text
    // Show that a buy offer with offerPrice < WAD can be taken with units > 0
    function testBugBuyMaxAssetsBypass() public {
        deal(address(loanToken), lender, 0); // lender pays 0
        collateralize(market, borrower, 100);

        lenderOffer.maxUnits = 0;
        lenderOffer.maxAssets = 1;
        lenderOffer.tick = MAX_TICK - 16; // offerPrice < WAD

        // Fully consume the offer before the take.
        vm.prank(lender);
        midnight.setConsumed(lenderOffer.group, lenderOffer.maxAssets, lender);

        uint256 lenderCreditBefore = midnight.creditOf(id, lender);
        uint256 borrowerDebtBefore = midnight.debtOf(id, borrower);
        uint256 totalUnitsBefore = midnight.totalUnits(id);
        uint256 lenderBalBefore = loanToken.balanceOf(lender);
        uint256 borrowerBalBefore = loanToken.balanceOf(borrower);

        (uint256 buyerAssets, uint256 sellerAssets) = take(1, borrower, lenderOffer);

        assertEq(buyerAssets, 0);
        assertEq(sellerAssets, 0);

        // Nothing observable to the cap or token balances changed:
        assertEq(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets);
        assertEq(loanToken.balanceOf(lender), lenderBalBefore);
        assertEq(loanToken.balanceOf(borrower), borrowerBalBefore);
        // But position state strictly changed:
        assertGt(midnight.creditOf(id, lender), lenderCreditBefore);
        assertGt(midnight.debtOf(id, borrower), borrowerDebtBefore);
        assertGt(midnight.totalUnits(id), totalUnitsBefore);
    }
```
