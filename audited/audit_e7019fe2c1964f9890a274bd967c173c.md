Audit Report

## Title
`claimContinuousFee` Reverts on Underflow When `withdrawable < continuousFeeCredit` - (File: `src/Midnight.sol`)

## Summary
`claimContinuousFee` unconditionally decrements `_marketState.withdrawable` by `amount` at line 320, but `_updatePosition` increases `continuousFeeCredit` without ever increasing `withdrawable`. In any market where borrowers have outstanding debt, `withdrawable` remains 0 while `continuousFeeCredit` grows, causing every `claimContinuousFee` call to revert with an arithmetic underflow. Accrued continuous fees are permanently unclaimable until borrowers repay in full.

## Finding Description
**Root cause — `claimContinuousFee` (`src/Midnight.sol:312-325`):**

```solidity
_marketState.continuousFeeCredit -= UtilsLib.toUint128(amount); // line 318
_marketState.totalUnits          -= UtilsLib.toUint128(amount); // line 319
_marketState.withdrawable        -= UtilsLib.toUint128(amount); // line 320 — no guard
```

The only pre-conditions are `msg.sender == feeClaimer` and `tickSpacing > 0`. There is no `require(amount <= _marketState.withdrawable)`. Solidity 0.8 checked arithmetic causes line 320 to revert whenever `withdrawable < amount`.

**How the counters diverge:**

`_updatePosition` (`src/Midnight.sol:846`) increases `continuousFeeCredit` by `accruedFee` but never touches `withdrawable`:

```solidity
marketState[id].continuousFeeCredit += UtilsLib.toUint128(accruedFee);
```

`withdrawable` only increases in `repay` (+`units`) and `liquidate` (+`repaidUnits`), as formally confirmed by the Certora rule `withdrawableUnchanged` (`certora/specs/WithdrawableMonotonicity.spec:45-57`), which explicitly excludes only those two functions (plus `withdraw` and `claimContinuousFee`) from the "unchanged" assertion.

**Exploit path (no privilege required):**

1. Lender provides `C` units via `take`. State: `withdrawable = 0`, `continuousFeeCredit = 0`.
2. Time passes. Anyone calls `updatePosition(market, lender)`. `continuousFeeCredit += fee`. `withdrawable` stays `0`.
3. Borrower does **not** repay. State: `continuousFeeCredit = fee > 0`, `withdrawable = 0`.
4. `feeClaimer` calls `claimContinuousFee(market, fee, receiver)` with `fee ≤ continuousFeeCredit`.
5. Line 318 (`continuousFeeCredit -= fee`) and line 319 (`totalUnits -= fee`) succeed.
6. Line 320 (`withdrawable -= fee`) → **arithmetic underflow → revert**.

**Corroborating evidence:**

- The Certora liveness rule in `certora/specs/Role.spec:264` asserts success requires `amount <= withdrawableBefore`, a constraint the code never enforces.
- `testClaimContinuousFee` (`test/ContinuousFeeTest.sol:420-423`) silently works around the bug by calling `repay` before claiming, with the comment *"Repay so withdrawable covers the claim."* No test exercises the claim-without-repay path.

## Impact Explanation
Accrued continuous fees are permanently unclaimable for any market where `withdrawable < continuousFeeCredit`. This is the default state of every active lending market from the moment fees begin accruing. The `feeClaimer` receives zero revenue from such markets until borrowers repay enough to bring `withdrawable ≥ amount`, which may never occur (defaulted borrowers, post-maturity bad debt). This constitutes a complete, indefinite loss of protocol fee revenue — a concrete, in-scope financial impact.

## Likelihood Explanation
No attacker action is required. The divergence between `continuousFeeCredit` and `withdrawable` is reached by ordinary protocol usage: any lend followed by time passing and `updatePosition` being called, with the borrower not yet repaying. This is the normal operating state of every active loan. The condition is persistent and self-reinforcing: the longer borrowers hold debt, the larger the gap grows.

## Recommendation
Add a guard in `claimContinuousFee` before decrementing `withdrawable`:

```solidity
require(amount <= _marketState.withdrawable, InsufficientWithdrawable());
```

Alternatively, if the design intent is that fees are claimable as they accrue (not just when borrowers repay), `_updatePosition` should also increment `withdrawable` by `accruedFee` to keep the two counters in sync. The chosen fix must be consistent with the invariant that `withdrawable` represents tokens actually present in the contract.

## Proof of Concept
```solidity
function testClaimContinuousFeeRevertsWithoutRepay(
    uint256 credit, uint256 feeRate, uint256 ttm, uint256 elapsed
) public {
    credit = bound(credit, 1, MAX_CREDIT);
    feeRate = bound(feeRate, 1, MAX_CONTINUOUS_FEE);
    ttm = bound(ttm, 2, 360 days);
    elapsed = bound(elapsed, 1, ttm - 1);

    setupLender(credit, feeRate, ttm);
    vm.warp(vm.getBlockTimestamp() + elapsed);
    midnight.updatePosition(market, lender);

    uint256 feeAmount = midnight.continuousFeeCredit(id);
    vm.assume(feeAmount > 0);
    // withdrawable is still 0 — borrower has not repaid
    assertEq(midnight.withdrawable(id), 0);

    // feeClaimer attempts to claim legitimately accrued fees
    vm.prank(feeClaimer);
    vm.expectRevert(); // arithmetic underflow on withdrawable -= amount
    midnight.claimContinuousFee(market, feeAmount, makeAddr("receiver"));
}
```

This test requires no repay call and directly demonstrates the revert. It can be added to `test/ContinuousFeeTest.sol` alongside the existing `testClaimContinuousFeeExcessReverts` test.