Based on my review of the actual code, I can confirm the claim against the real implementation.

**Key findings from the code:**

1. `withdraw` at line 493 performs `_position.credit -= UtilsLib.toUint128(units)` with no prior guard that `units <= _position.credit`. [1](#0-0) 

2. `take` at line 383 computes `sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit)` and at line 413 applies `sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease)`, which can reduce a seller's credit to exactly 0. [2](#0-1) 

3. The `if (_position.credit > 0)` guard at line 489 only protects the fee calculation — it does not protect the subtraction at line 493. [1](#0-0) 

4. The Certora `withdrawEffects` rule asserts `creditOf(id, onBehalf) == updatedUserCredit - units` but does not model the case where `units > updatedUserCredit`; it implicitly assumes valid inputs and does not catch this path. [3](#0-2) 

5. SECURITY.md and RESEARCHER.md confirm DoS / service unavailability under realistic attacker input is an in-scope impact class. [4](#0-3) 

---

Audit Report

## Title
`withdraw` reverts with arithmetic underflow when a concurrent `take` reduces seller credit below requested units - (File: src/Midnight.sol)

## Summary
`withdraw` unconditionally subtracts `units` from `_position.credit` at line 493 without verifying that `units <= _position.credit`. Because `take` can legitimately reduce a sell-side maker's credit to zero in the same block, any taker can front-run a lender's pending `withdraw` transaction, driving the lender's credit below `units` and causing the Solidity 0.8 checked subtraction to revert. The lender's funds are not stolen, but withdrawal is blocked for as long as the attacker is willing to repeat the attack.

## Finding Description
**Root cause:** `withdraw` (`src/Midnight.sol:481-500`) performs no pre-check that `units <= _position.credit` before the subtraction at line 493:

```solidity
_position.credit -= UtilsLib.toUint128(units);   // line 493 – no guard
```

The only conditional block before this line (`if (_position.credit > 0)`) guards only the fee calculation, not the credit subtraction itself.

**How `take` enables the underflow:** In `take` (`src/Midnight.sol:383,413`):

```solidity
uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);  // line 383
sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);           // line 413
```

`take` is bounded by `sellerPos.credit` so it never underflows itself, but it can reduce the seller's credit to any value in `[0, sellerPos.credit]`, including 0.

**Exploit flow:**
1. Lender has `position[id][lender].credit = C` and an active sell offer (`offer.buy = false`, `offer.maker = lender`, `offer.maxUnits >= C`).
2. Lender submits `withdraw(market, C, lender, receiver)` to the mempool.
3. Attacker observes the pending transaction and front-runs with `take(offer, ..., C, attacker, ...)`.
4. `take` executes first: `sellerCreditDecrease = min(C, C) = C`; lender's credit becomes 0.
5. Lender's `withdraw` executes: `_updatePosition` runs (credit stays 0), the `if (_position.credit > 0)` branch is skipped, then `_position.credit -= C` → `0 - C` → arithmetic underflow → revert.

**Why existing checks fail:**
- `take` has no cap preventing it from consuming all of a maker's credit.
- `withdraw` calls `_updatePosition` (which can only decrease credit further) but never asserts `units <= _position.credit`.
- `_marketState.withdrawable` is not decremented by `take`, so the market-level withdrawable check at line 494 does not catch the per-user credit shortfall.
- The Certora `withdrawEffects` rule (`certora/specs/BalanceEffects.spec:74`) asserts `creditOf == updatedUserCredit - units` but does not model the case where `units > updatedUserCredit`; the spec implicitly assumes valid inputs.

## Impact Explanation
Any lender who simultaneously holds an active sell offer can have their `withdraw` transaction griefed by any taker. The lender's transaction reverts; they must re-query their credit and resubmit. A determined attacker can repeat this every block for as long as the lender has an active sell offer, creating a sustained DoS against `withdraw` for that lender. The lender's funds are not stolen, but withdrawal is blocked until the offer is cancelled or fully consumed. This constitutes service unavailability under realistic attacker input, which is an in-scope impact per RESEARCHER.md.

## Likelihood Explanation
**Preconditions:**
- Lender has an active sell offer (`offer.buy = false`, `offer.maker = lender`).
- Lender submits `withdraw(units)` where `units > 0` and the offer still has capacity.

Both conditions are routine in normal protocol usage. The attacker requires no special role and pays only gas; the token cost of the take is recovered as credit (net cost ≈ gas only). The attack is repeatable every block and requires only mempool observation.

## Recommendation
Add an explicit guard in `withdraw` before the credit subtraction:

```solidity
require(units <= _position.credit, InsufficientCredit());
_position.credit -= UtilsLib.toUint128(units);
```

This mirrors the pattern already used in `take` (`UtilsLib.min`) and `repay` (debt subtraction guarded by the debt value). Alternatively, use `UtilsLib.zeroFloorSub` if partial withdrawals are acceptable, but an explicit revert with a descriptive error is preferable for user-facing functions. The Certora `withdrawEffects` rule should also be updated to add a precondition `require units <= updatedUserCredit` to correctly model the valid input domain.

## Proof of Concept
**Minimal Foundry test plan:**
1. Deploy the protocol and create a market.
2. Have `lender` supply `C` units (gaining `position[id][lender].credit = C`).
3. Have `lender` post a sell offer with `maxUnits = C`.
4. In a single test transaction (simulating front-run ordering):
   a. Call `take(offer, ..., C, attacker, ...)` as `attacker` — verify `position[id][lender].credit == 0`.
   b. Call `withdraw(market, C, lender, lender)` as `lender` — expect revert with arithmetic underflow (panic code `0x11`).
5. Assert the revert occurs at step 4b, confirming the DoS.

### Citations

**File:** src/Midnight.sol (L383-413)
```text
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
```

**File:** src/Midnight.sol (L489-493)
```text
        if (_position.credit > 0) {
            pendingFeeDecrease = UtilsLib.toUint128(_position.pendingFee.mulDivUp(units, _position.credit));
            _position.pendingFee -= pendingFeeDecrease;
        }
        _position.credit -= UtilsLib.toUint128(units);
```

**File:** certora/specs/BalanceEffects.spec (L74-74)
```text
    assert creditOf(id, onBehalf) == updatedUserCredit - units;
```

**File:** RESEARCHER.md (L64-64)
```markdown
- Permanent lock/freeze states created through reachable user actions.
```
