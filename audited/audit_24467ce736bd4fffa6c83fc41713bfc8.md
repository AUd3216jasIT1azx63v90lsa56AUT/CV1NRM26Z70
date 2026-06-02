Audit Report

## Title
Off-by-One in Maturity Guard Allows Debt Increase at Exact Maturity Timestamp - (`src/Midnight.sol`)

## Summary
The guard at `src/Midnight.sol:391` uses `block.timestamp <= offer.market.maturity`, which evaluates to `true` at exact equality, permitting `sellerDebtIncrease > 0` when `block.timestamp == market.maturity`. This directly violates the protocol's own invariant that explicitly names timestamp equality as a forbidden path for debt increase at the maturity boundary. The fee calculation already treats this timestamp as post-maturity (returning `timeToMaturity = 0`), creating an internal inconsistency.

## Finding Description

**Root cause — `<=` instead of `<` in the maturity guard:** [1](#0-0) 

When `block.timestamp == market.maturity`, the left operand `block.timestamp <= offer.market.maturity` is `true`, so the `require` passes unconditionally regardless of `sellerDebtIncrease`.

**`zeroFloorSub` treats equality as post-maturity for fee purposes:** [2](#0-1) 

`gt(maturity, maturity)` evaluates to `0`, so `timeToMaturity = 0` at `block.timestamp == maturity`. The fee calculation at line 359–386 already treats this timestamp identically to any post-maturity block — `buyerPendingFeeIncrease = 0` — but the debt guard does not enforce the same boundary. [3](#0-2) 

**Exploit flow:**

1. A sell offer exists (`offer.buy == false`, maker = borrower) with `offer.expiry >= market.maturity`.
2. The seller has zero credit (`sellerPos.credit == 0`), so `sellerCreditDecrease = 0` and `sellerDebtIncrease = units`.
3. Taker waits for `block.timestamp == market.maturity`.
4. Taker calls `take(sellOffer, ..., units, taker, ...)`.
5. Line 391: `maturity <= maturity` → `true` → passes.
6. Line 414: `sellerPos.debt += units` — debt is increased at maturity. [4](#0-3) 

**Why existing checks do not stop it:**

- `block.timestamp <= offer.expiry` (line 353): passes if `expiry >= maturity`.
- The maturity guard (line 391) uses `<=` not `<`.
- Post-maturity liquidation (line 622) requires `block.timestamp > market.maturity` (strict `>`), so the newly created debt cannot be liquidated in post-maturity mode in the same block. [5](#0-4) 

**Violated invariants (from `live_context.json`):** [6](#0-5) [7](#0-6) [8](#0-7) 

**Existing tests only cover `maturity + 1`, not `maturity` exactly:** [9](#0-8) 

## Impact Explanation
A borrower's debt is increased at `block.timestamp == market.maturity` with zero fee accrual (fee calculation returns 0 for `timeToMaturity = 0`), violating the core credit/debt accounting invariant. The position is immediately subject to post-maturity liquidation in the next block with no recourse for the borrower. This constitutes credit/debt accounting corruption and can be used to force a borrower into an immediately-liquidatable state, enabling unauthorized collateral seizure.

## Likelihood Explanation
Any block whose `block.timestamp` equals a market's maturity is sufficient. On PoS chains, validators can influence `block.timestamp` within a small window; a taker can also simply submit the transaction and have it land in the correct block. The precondition — a sell offer with `expiry >= maturity` — is normal and expected. The attack is repeatable for every market whose maturity falls on a block timestamp.

## Recommendation
Change `<=` to `<` at line 391:

```solidity
// Before (vulnerable):
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());

// After (fixed):
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

This aligns the debt-increase guard with the fee calculation, which already treats `block.timestamp == maturity` as post-maturity via `zeroFloorSub`.

## Proof of Concept

Add the following test to `test/TakeTest.sol`:

```solidity
function testBuyAtExactMaturity() public {
    uint256 units = 100;
    uint256 timestamp = market.maturity; // exact equality
    vm.warp(timestamp);
    borrowerOffer.expiry = timestamp;    // expiry >= maturity passes line 353
    borrowerOffer.maxUnits = units;
    deal(address(loanToken), lender, units);
    collateralize(market, borrower, units);

    // Should revert with CannotIncreaseDebtPostMaturity but does NOT with <= guard
    vm.expectRevert(IMidnight.CannotIncreaseDebtPostMaturity.selector);
    take(units, lender, borrowerOffer);
}
```

With the current `<=` guard, this test **fails** (no revert occurs and debt is increased). After changing to `<`, the test passes. This mirrors the existing `testBuy1PostMaturity` pattern at line 315 which uses `maturity + 1` and correctly reverts — the gap is exactly the `maturity` boundary itself.

### Citations

**File:** src/Midnight.sol (L359-386)
```text
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
        uint256 _settlementFee = settlementFee(id, timeToMaturity);
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
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

        (address buyer, address seller) = offer.buy ? (offer.maker, taker) : (taker, offer.maker);
        Position storage buyerPos = position[id][buyer];
        Position storage sellerPos = position[id][seller];

        if (hasCredit(id, buyer) || units > buyerPos.debt) _updatePosition(offer.market, id, buyer);
        if (hasCredit(id, seller)) _updatePosition(offer.market, id, seller);

        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
        uint128 buyerPendingFeeIncrease =
            UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
```

**File:** src/Midnight.sol (L391-391)
```text
        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

**File:** src/Midnight.sol (L414-414)
```text
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/libraries/UtilsLib.sol (L22-26)
```text
    function zeroFloorSub(uint256 x, uint256 y) internal pure returns (uint256 z) {
        assembly {
            z := mul(gt(x, y), sub(x, y))
        }
    }
```

**File:** live_context.json (L86-86)
```json
      "debt_increase_after_maturity_forbidden": true,
```

**File:** live_context.json (L197-197)
```json
      "debt must not increase after maturity"
```

**File:** live_context.json (L221-221)
```json
      "maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing",
```

**File:** test/TakeTest.sol (L315-326)
```text
    function testBuy1PostMaturity() public {
        uint256 units = 100;
        uint256 timestamp = market.maturity + 1;
        vm.warp(timestamp);
        borrowerOffer.expiry = timestamp;
        borrowerOffer.maxUnits = units;
        deal(address(loanToken), lender, units);
        collateralize(market, borrower, units);

        vm.expectRevert(IMidnight.CannotIncreaseDebtPostMaturity.selector);
        take(units, lender, borrowerOffer);
    }
```
