Audit Report

## Title
Off-by-one in `CannotIncreaseDebtPostMaturity` guard allows new debt at `block.timestamp == maturity` - (File: src/Midnight.sol)

## Summary
The guard at `src/Midnight.sol:391` uses `block.timestamp <= offer.market.maturity`, which evaluates to `true` at the exact maturity timestamp, allowing `sellerDebtIncrease > 0` to pass unchecked. This directly violates the protocol's own invariant at `live_context.json:221` that explicitly forbids debt increase via "timestamp equality" at the maturity boundary. Debt created at this instant is immediately overdue with zero repayment window, inflating `totalUnits` and creating bad debt risk.

## Finding Description
In `take()` (`src/Midnight.sol:337`), after computing `sellerDebtIncrease = units - sellerCreditDecrease` at line 384, the guard is:

```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
``` [1](#0-0) 

The logical OR short-circuits: when `block.timestamp == offer.market.maturity`, the left operand is `true`, so the require passes regardless of `sellerDebtIncrease`. The intended invariant — no new debt at or after maturity — is only enforced for `block.timestamp > maturity`.

This directly contradicts the protocol's stated invariant:

> "maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing" [2](#0-1) 

And the credit/debt invariant:

> "debt must not increase after maturity" [3](#0-2) 

**Exploit flow:**
1. Maker creates a sell offer (`offer.buy = false`) with `offer.expiry >= market.maturity` and no existing credit in the market (ensuring `sellerCreditDecrease = 0`, so `sellerDebtIncrease = units`).
2. Maker ratifies the offer via a `SetterRatifier` — a fully permissionless action.
3. Taker waits until `block.timestamp == market.maturity` (achievable by a validator or natural block alignment).
4. Taker calls `take()`. The expiry check (`block.timestamp <= offer.expiry`) passes. At line 391, `block.timestamp <= offer.market.maturity` is `true` (equality), so the require does not revert even with `sellerDebtIncrease = units > 0`.
5. Lines 414 and 416–417 execute: `sellerPos.debt += units` and `totalUnits += buyerCreditIncrease - sellerCreditDecrease`. [4](#0-3) 

**Why existing checks fail:**
- `CannotIncreaseDebtPostMaturity` only triggers when `block.timestamp > maturity` AND `sellerDebtIncrease != 0`; the `==` case is excluded by `<=`.
- `timeToMaturity = zeroFloorSub(maturity, maturity) = 0` at line 359, so `buyerPendingFeeIncrease = 0` — no fee is charged, making the attack costless. [5](#0-4) 

- No other check in `take()` blocks debt creation at exactly maturity.

## Impact Explanation
New debt units are written to `sellerPos.debt` and `totalUnits` is incremented at the exact maturity timestamp. The debt is immediately overdue (liquidatable) with zero repayment window. The corresponding `buyerCreditIncrease` represents a claim on loan tokens that must be backed by repayment of the newly created debt. If the debt becomes bad debt, `totalUnits` is inflated relative to the recoverable loan token balance, directly violating the solvency invariant: "contract token balances must cover withdrawable assets, collateral claims, credit redemptions, and accrued fees." [6](#0-5) 

This falls squarely within the in-scope impact class "bad debt creation." [7](#0-6) 

## Likelihood Explanation
The precondition requires `block.timestamp == market.maturity` exactly. On Ethereum, block timestamps are validator-controlled within a range; a validator or a searcher colluding with a validator can target a specific timestamp. Market maturities are set at creation and are publicly known, making them targetable. The offer setup is fully permissionless. The attack is repeatable across any market whose maturity aligns with a block timestamp, which is a common occurrence for round-number maturities (e.g., end-of-month or end-of-year Unix timestamps).

## Recommendation
Change `<=` to `<` in the guard at `src/Midnight.sol:391`:

```solidity
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
``` [1](#0-0) 

This aligns the code with the protocol's explicit invariant that "timestamp equality" at the maturity boundary must not permit debt increase.

## Proof of Concept
```solidity
function testDebtAtExactMaturity() public {
    // Setup: create market with maturity = T
    uint256 maturity = block.timestamp + 1000;
    // ... create market, create sell offer with expiry >= maturity, no existing credit
    
    // Warp to exact maturity
    vm.warp(maturity);
    
    // take() should revert with CannotIncreaseDebtPostMaturity but does not
    vm.expectRevert(CannotIncreaseDebtPostMaturity.selector); // This expectation FAILS
    midnight.take(offer, ratifierData, units, taker, receiver, address(0), "");
    
    // Verify debt was incorrectly created
    assertGt(midnight.position(id, seller).debt, 0);
}
```

All existing post-maturity tests use `maturity + 1` or `bound(maturity, 0, vm.getBlockTimestamp() - 1)`, leaving the exact equality case untested. [8](#0-7)

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

**File:** src/Midnight.sol (L414-417)
```text
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);

        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
```

**File:** live_context.json (L56-56)
```json
      "bad debt creation",
```

**File:** live_context.json (L188-188)
```json
      "contract token balances must cover withdrawable assets, collateral claims, credit redemptions, and accrued fees",
```

**File:** live_context.json (L197-197)
```json
      "debt must not increase after maturity"
```

**File:** live_context.json (L219-222)
```json
    "maturity": [
      "pre-maturity and post-maturity paths must not be composable to bypass debt repayment",
      "maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing",
      "post-maturity unwinding must not mint free credit or erase debt incorrectly"
```
