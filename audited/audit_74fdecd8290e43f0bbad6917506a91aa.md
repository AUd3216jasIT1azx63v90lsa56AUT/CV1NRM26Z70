Audit Report

## Title
Off-by-one in `CannotIncreaseDebtPostMaturity` guard allows new debt at `block.timestamp == maturity` - (File: src/Midnight.sol)

## Summary
The guard at `src/Midnight.sol:391` uses `block.timestamp <= offer.market.maturity`, which evaluates to `true` at the exact maturity timestamp, permitting `sellerDebtIncrease > 0` to pass unchecked. This directly contradicts the protocol's own invariant (`live_context.json:221`) that explicitly forbids debt increase by "timestamp equality" at the maturity boundary. Debt created at this instant is immediately overdue with zero repayment window and zero fee, creating bad debt risk and inflating `totalUnits`.

## Finding Description
In `take()` (`src/Midnight.sol:337`), `sellerDebtIncrease` is computed at line 384 as `units - sellerCreditDecrease`. The guard is:

```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
``` [1](#0-0) 

The logical OR short-circuits: when `block.timestamp == offer.market.maturity`, the left operand is `true`, so the require passes regardless of `sellerDebtIncrease`. The intended invariant — no new debt at or after maturity — is only enforced for `block.timestamp > maturity`.

The protocol's own invariant document explicitly names this case as forbidden: [2](#0-1) [3](#0-2) 

At `block.timestamp == maturity`, `timeToMaturity = zeroFloorSub(maturity, maturity) = 0` (line 359), so `buyerPendingFeeIncrease = 0` — no fee is charged, making the attack costless to the attacker. [4](#0-3) 

State writes at lines 414 and 416–417 execute unconditionally once the guard passes: [5](#0-4) 

**Why existing checks fail:**
- `CannotIncreaseDebtPostMaturity` only triggers when `block.timestamp > maturity` AND `sellerDebtIncrease != 0`; the `==` case is excluded by `<=`.
- The health check at line 476 (`isHealthy`) checks collateral coverage, not whether debt is overdue. A well-collateralized seller passes this check even at maturity.
- `offer.reduceOnly` is maker-controlled and not enforced by the protocol.
- All existing post-maturity tests use `maturity + 1`, never `maturity` exactly (e.g., lines 317, 330, 349, 667, 680), leaving the `==` case untested. [6](#0-5) [7](#0-6) 

**Exploit flow:**
1. Attacker (seller/maker) creates a sell offer (`offer.buy = false`) with `offer.expiry >= market.maturity` and no existing credit in the market (ensuring `sellerCreditDecrease = 0`, so `sellerDebtIncrease = units`). Attacker supplies sufficient collateral to pass the health check.
2. Attacker ratifies the offer via a `SetterRatifier` — a permissionless action.
3. Attacker (or colluding validator) waits until `block.timestamp == market.maturity`.
4. Taker calls `take()`. At line 391, `block.timestamp <= offer.market.maturity` is `true` (equality), so the require does not revert even with `sellerDebtIncrease = units > 0`.
5. Lines 414 and 416–417 execute: `sellerPos.debt += units` and `totalUnits += buyerCreditIncrease`. The seller receives loan tokens; the debt is immediately overdue.

## Impact Explanation
New debt units are written to `sellerPos.debt` and `totalUnits` is incremented at the exact maturity timestamp. The debt is immediately overdue (liquidatable even if healthy, per `live_context.json:88`) with zero repayment window. The corresponding `buyerCreditIncrease` represents a claim on loan tokens that must be backed by repayment of the newly created debt. If the debt becomes bad debt, `totalUnits` is inflated relative to the recoverable loan token balance, directly violating the solvency invariant: "contract token balances must cover withdrawable assets, collateral claims, credit redemptions, and accrued fees." [8](#0-7) [9](#0-8) 

## Likelihood Explanation
The precondition requires `block.timestamp == market.maturity` exactly. On Ethereum, block timestamps are validator-controlled within a range; a validator or a searcher colluding with a validator can target a specific timestamp. Market maturities are set at creation and are publicly known, making them targetable. The offer setup is fully permissionless. The attack is repeatable across any market whose maturity aligns with a block timestamp. [10](#0-9) 

## Recommendation
Change the guard from `<=` to `<`:

```solidity
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

This ensures that at `block.timestamp == maturity`, the left operand is `false`, forcing `sellerDebtIncrease == 0` to be checked, consistent with the protocol invariant that the maturity boundary itself must not allow debt increase. [1](#0-0) 

## Proof of Concept
Add to `TakeTest.sol`:

```solidity
function testBuyAtExactMaturity() public {
    uint256 units = 100;
    uint256 timestamp = market.maturity; // exactly at maturity, not +1
    vm.warp(timestamp);
    borrowerOffer.expiry = timestamp;
    borrowerOffer.maxUnits = units;
    deal(address(loanToken), lender, units);
    collateralize(market, borrower, units);

    // This should revert with CannotIncreaseDebtPostMaturity but currently does NOT
    vm.expectRevert(IMidnight.CannotIncreaseDebtPostMaturity.selector);
    take(units, lender, borrowerOffer);
}
```

This test will fail (no revert) with the current `<=` guard, confirming the bug. Changing `<=` to `<` makes it pass. [11](#0-10)

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

**File:** src/Midnight.sol (L476-476)
```text
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

**File:** live_context.json (L86-88)
```json
      "debt_increase_after_maturity_forbidden": true,
      "post_maturity_trading_purpose": "facilitate unwinding when liquidations are unprofitable",
      "overdue_debt_after_maturity_is_liquidatable_even_if_healthy": true
```

**File:** live_context.json (L187-190)
```json
    "solvency": [
      "contract token balances must cover withdrawable assets, collateral claims, credit redemptions, and accrued fees",
      "total claimable credit must not exceed repaid loan assets plus valid recoverable debt after loss accounting",
      "bad debt must reduce lender credit exactly once and proportionally"
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

**File:** test/TakeTest.sol (L666-677)
```text
    function testBuyPastMaturity(uint256 timestamp) public {
        timestamp = bound(timestamp, market.maturity + 1, type(uint32).max);
        vm.warp(timestamp);
        borrowerOffer.expiry = timestamp;
        borrowerOffer.maxUnits = 100;
        borrowerOffer.tick = MAX_TICK;
        deal(address(loanToken), lender, 100);
        collateralize(market, borrower, 100);

        vm.expectRevert(IMidnight.CannotIncreaseDebtPostMaturity.selector);
        take(100, lender, borrowerOffer);
    }
```
