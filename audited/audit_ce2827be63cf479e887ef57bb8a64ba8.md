All code references check out. The test `testBugBuyMaxAssetsBypass` exists and confirms the exact behavior described. Here is the validated report:

---

Audit Report

## Title
Zero-asset rounding allows unbounded `maxAssets` offer reuse with no token transfer — (`src/Midnight.sol`, `src/libraries/UtilsLib.sol`)

## Summary
`mulDivDown` in `UtilsLib` truncates to zero when `units * offerPrice < WAD`. For buy offers with `maxAssets > 0`, `consumed` is incremented by `buyerAssets` (which is 0), so the cap check never advances and the offer is never exhausted. Position credit and debt still increase via `units`-based arithmetic, and no tokens are transferred, creating unbacked credit and debt without bound.

## Finding Description

**Root cause — `UtilsLib.mulDivDown` truncates to zero:**

`src/libraries/UtilsLib.sol` lines 29–31:
```solidity
function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y) / d;
}
```
When `x * y < d`, the result is 0.

**Vulnerable code path — `src/Midnight.sol` lines 363–373:**

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // → 0 when units * offerPrice < WAD
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    //                                                              ^^^^^^^^^^^ += 0
    require(newConsumed <= offer.maxAssets, ConsumedAssets());   // passes unchanged
}
```

For a buy offer, `buyerPrice = offerPrice` (settlement fee cancels). When `units * offerPrice < WAD`, `buyerAssets = 0`. `consumed` does not advance; the `require` passes because the value is unchanged.

**Tick range where this applies:** `tickToPrice` returns values in `[0, WAD]`. For any tick below `MAX_TICK` (5820), `offerPrice < WAD`, so there always exist `units` values satisfying `units * offerPrice < WAD`. At `tick = MAX_TICK - 16`, `units = 1` is sufficient.

**Position state still mutates with `units` (lines 382–414):**
```solidity
uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
uint256 sellerDebtIncrease  = units - sellerCreditDecrease;
...
buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);
sellerPos.debt  += UtilsLib.toUint128(sellerDebtIncrease);
```
These use `units` directly, not `buyerAssets`, so credit/debt changes occur even when `buyerAssets = 0`.

**Token transfers are zero (lines 455–456):**
```solidity
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets); // 0
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);                    // 0
```
Both `buyerAssets` and `sellerAssets` are 0, so no tokens move.

**Why existing checks fail:**
- `require(newConsumed <= offer.maxAssets)` only enforces an upper bound; it does not require that `consumed` actually increased.
- There is no `require(units == 0 || buyerAssets > 0)` guard anywhere in `take`.
- The `maxUnits` branch (line 371) uses `+= units` and is immune; only the `maxAssets` branch is affected.

## Impact Explanation
Every call with `units = U > 0` and `buyerAssets = 0`:
- Grants the buyer (maker) `U` units of credit with zero token payment.
- Burdens the seller (taker) with `U` units of debt with zero token receipt.
- Leaves `consumed` unchanged, so the offer is never exhausted regardless of `maxAssets`.

Repeated indefinitely, this creates unbounded credit for the maker and unbounded debt for the taker without any token backing, directly violating the solvency invariant: the contract's loan-token balance does not cover the credit it has issued. `totalUnits` also grows without bound, corrupting market accounting.

## Likelihood Explanation
**Preconditions:**
- A buy offer with `maxAssets > 0` must exist (common configuration).
- The offer's tick must satisfy `offerPrice < WAD` — true for every valid tick below `MAX_TICK` (5820), i.e., essentially all real offers.
- `units * offerPrice < WAD` for some reachable `units > 0` — true for any tick where `offerPrice < WAD`.

**Feasibility:** Any unprivileged taker can compute the required `units` off-chain as `U = floor((WAD − 1) / offerPrice)` and call `take` directly. No special permissions, oracle manipulation, or token owner cooperation is required. The taker need not hold any tokens.

**Repeatability:** Unlimited — `consumed` never advances, so the offer never closes. The attack can be repeated in every block.

## Recommendation
Add a guard in the `maxAssets` branch of `take` that rejects a non-zero `units` call when the resulting asset amount rounds to zero:

```solidity
if (offer.maxAssets > 0) {
    require(units == 0 || (offer.buy ? buyerAssets : sellerAssets) > 0, ZeroAssets());
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, enforce a minimum `units` value such that the computed asset amount is always non-zero, or switch the `consumed` tracking to always use `units` (as the `maxUnits` branch does) and expose a separate `maxUnits`-equivalent cap denominated in assets.

## Proof of Concept
The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` lines 857–889 is a complete, self-contained reproduction:

1. Set `lenderOffer.maxAssets = 1`, `lenderOffer.tick = MAX_TICK - 16`.
2. Pre-fill `consumed` to `maxAssets` via `midnight.setConsumed(...)`.
3. Call `take(1, borrower, lenderOffer)`.
4. Assert `buyerAssets == 0`, `sellerAssets == 0`, `consumed` unchanged at `maxAssets`.
5. Assert `creditOf(lender)`, `debtOf(borrower)`, and `totalUnits` all strictly increased.

The test passes, confirming the exploit is live in the current codebase. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/libraries/UtilsLib.sol (L28-31)
```text
    /// @dev Returns (x * y) / d rounded down.
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
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

**File:** src/Midnight.sol (L455-456)
```text
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
