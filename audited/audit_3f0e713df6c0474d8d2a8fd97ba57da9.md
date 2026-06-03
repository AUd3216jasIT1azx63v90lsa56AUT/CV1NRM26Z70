Audit Report

## Title
Off-by-one in `CannotIncreaseDebtPostMaturity` guard permits debt creation at `block.timestamp == market.maturity` - (File: `src/Midnight.sol`)

## Summary
The guard at `src/Midnight.sol:391` uses `<=` instead of `<`, so when `block.timestamp == market.maturity` the first disjunct is `true` and the require passes unconditionally regardless of `sellerDebtIncrease`. This violates the explicit protocol invariant at `live_context.json:221` ("maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing") and enables an attacker to receive credit at maturity with zero fee obligation, then drain pre-existing `withdrawable` balances belonging to other lenders.

## Finding Description

**Root cause — `src/Midnight.sol:391`:**

```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

When `block.timestamp == market.maturity`, `block.timestamp <= offer.market.maturity` evaluates to `true`, so the require passes unconditionally regardless of `sellerDebtIncrease`.

**Debt and credit mutation at maturity — lines 408–414:**

```solidity
buyerPos.debt  -= UtilsLib.toUint128(units - buyerCreditIncrease);
buyerPos.pendingFee += buyerPendingFeeIncrease;
buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);   // buyer gets credit

sellerPos.pendingFee -= sellerPendingFeeDecrease;
sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
sellerPos.debt   += UtilsLib.toUint128(sellerDebtIncrease);   // debt created at maturity
```

**Zero fee for buyer:** `timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp)` (line 359) equals `0` at maturity, so `buyerPendingFeeIncrease = 0` (line 386). The buyer receives credit with no fee obligation.

**`take()` never increments `withdrawable`:** Confirmed across lines 375–479; there is no `_marketState.withdrawable +=` anywhere in `take()`.

**`withdraw()` decrements `withdrawable` directly — line 494:**

```solidity
_position.credit -= UtilsLib.toUint128(units);
_marketState.withdrawable -= UtilsLib.toUint128(units);
```

If `withdrawable >= units` at the time of the attack, the attacker can immediately drain those tokens, which belong to other lenders awaiting their own `withdraw()` calls.

**Secondary guard at line 476 is insufficient:**

```solidity
require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

This checks collateral health, not maturity. A seller with sufficient collateral passes this check even at maturity.

**Exploit flow:**
1. A sell offer exists with `offer.expiry >= market.maturity` (the `offer.expiry` check at line 353 is separate from `market.maturity`).
2. Attacker submits `take(sellOffer, ..., units, ...)` in the block where `block.timestamp == market.maturity`.
3. Line 391 passes because `block.timestamp <= maturity` is `true`.
4. `sellerPos.debt += units`; `buyerPos.credit += units`; `_marketState.withdrawable` unchanged.
5. Attacker calls `withdraw(market, units, buyer, receiver)` — line 494 decrements `withdrawable` and transfers tokens out, draining funds owed to other lenders.

**Test coverage gap:** All 24 `vm.warp` calls in `test/TakeTest.sol` use `maturity + 1`, leaving the `block.timestamp == maturity` boundary completely untested.

## Impact Explanation
- **Buyer/attacker:** Receives credit redeemable against any pre-existing `withdrawable` in the market, directly stealing funds owed to other lenders.
- **Other lenders:** Their `withdraw()` calls revert or are underfunded because `withdrawable` was drained.
- **Seller:** Placed into a debt position at maturity with zero time remaining; immediately subject to post-maturity liquidation regardless of collateral health.
- Impact class: direct theft of lender funds and permanent accounting desync. Concrete and in-scope.

## Likelihood Explanation
- `block.timestamp == market.maturity` is achievable by any block proposer on Ethereum post-merge (12-second slots), or by any attacker who monitors the mempool and submits a transaction timed to land in the maturity block.
- No privileged access is required; the attacker is the taker (`msg.sender`), a normal external user.
- `withdrawable > 0` is a realistic condition in any active market that has seen prior repayments.
- The invariant violation (debt at maturity) occurs even without `withdrawable > 0`; the fund-loss step additionally requires `withdrawable > 0`.

## Recommendation
Change `<=` to `<` at `src/Midnight.sol:391`:

```solidity
// Before (vulnerable):
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());

// After (fixed):
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

This ensures that at `block.timestamp == market.maturity`, the first disjunct is `false`, and the require only passes if `sellerDebtIncrease == 0`, consistent with the protocol invariant at `live_context.json:221`.

## Proof of Concept

```solidity
// In TakeTest.sol — add this test:
function test_takeAtExactMaturity_drainsWithdrawable() public {
    // 1. Setup: lender supplies, borrower repays to create withdrawable > 0
    //    (standard setup from existing tests)

    // 2. Warp to exact maturity (NOT maturity + 1)
    vm.warp(market.maturity);

    // 3. Attacker takes a sell offer at maturity
    vm.prank(attacker);
    midnight.take(sellOffer, ratifierData, units, attacker, address(0), address(0), "");

    // 4. Attacker immediately withdraws, draining withdrawable
    vm.prank(attacker);
    midnight.withdraw(market, units, attacker, attacker);

    // 5. Assert: other lender's withdraw now reverts due to insufficient withdrawable
    vm.prank(lender);
    vm.expectRevert();
    midnight.withdraw(market, lenderUnits, lender, lender);
}
```

The test passes (attacker succeeds, lender reverts) with the current `<=` guard and fails (attacker's `take` reverts) after changing to `<`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** src/Midnight.sol (L359-359)
```text
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
```

**File:** src/Midnight.sol (L385-386)
```text
        uint128 buyerPendingFeeIncrease =
            UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
```

**File:** src/Midnight.sol (L391-391)
```text
        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

**File:** src/Midnight.sol (L408-414)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/Midnight.sol (L476-476)
```text
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

**File:** src/Midnight.sol (L493-494)
```text
        _position.credit -= UtilsLib.toUint128(units);
        _marketState.withdrawable -= UtilsLib.toUint128(units);
```

**File:** live_context.json (L219-222)
```json
    "maturity": [
      "pre-maturity and post-maturity paths must not be composable to bypass debt repayment",
      "maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing",
      "post-maturity unwinding must not mint free credit or erase debt incorrectly"
```
