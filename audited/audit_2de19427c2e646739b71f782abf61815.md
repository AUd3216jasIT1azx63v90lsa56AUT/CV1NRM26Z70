All key claims check out against the actual code. Here is the validated report:

---

Audit Report

## Title
Continuous Fee Unclaimable When `withdrawable < continuousFeeCredit` Due to Missing `withdrawable` Increase in `_updatePosition` - (File: src/Midnight.sol)

## Summary
`_updatePosition` increases `marketState[id].continuousFeeCredit` by the accrued fee but never increases `withdrawable`. Because `claimContinuousFee` decrements `withdrawable` unconditionally, any call where `amount > withdrawable` reverts via checked-arithmetic underflow on line 320. The fee claimer therefore has no unilateral ability to extract accrued continuous fees while borrower debt remains outstanding.

## Finding Description

**Root cause — `_updatePosition` does not touch `withdrawable`:**

`_updatePosition` at `src/Midnight.sol:842-846` reduces the lender's `credit` by `accruedFee` and credits that amount to `continuousFeeCredit`, but leaves `withdrawable` unchanged:

```solidity
_position.credit = newCredit;
...
marketState[id].continuousFeeCredit += UtilsLib.toUint128(accruedFee);
``` [1](#0-0) 

The Certora rule `withdrawableUnchanged` in `certora/specs/WithdrawableMonotonicity.spec` formally verifies this: it asserts `withdrawable` is unchanged for every non-view function *except* `repay`, `liquidate`, `withdraw`, and `claimContinuousFee`. `updatePosition` is not excluded, confirming it provably leaves `withdrawable` unchanged. [2](#0-1) 

The only functions that increase `withdrawable` are `repay` and `liquidate`, as formally verified by `repayIncreasesWithdrawable` and `liquidateIncreasesWithdrawable`: [3](#0-2) 

**Revert path in `claimContinuousFee`:**

`claimContinuousFee` at `src/Midnight.sol:318-320` performs three unchecked decrements with no guard on `withdrawable`:

```solidity
_marketState.continuousFeeCredit -= UtilsLib.toUint128(amount);
_marketState.totalUnits          -= UtilsLib.toUint128(amount);
_marketState.withdrawable        -= UtilsLib.toUint128(amount);  // ← underflow revert
``` [4](#0-3) 

There is no `require(amount <= withdrawable)` guard. The revert is a Solidity checked-arithmetic underflow.

**Test confirmation:**

`testClaimContinuousFee` in `test/ContinuousFeeTest.sol` explicitly calls `repay` before `claimContinuousFee` with the comment *"Repay so withdrawable covers the claim"*, directly confirming the authors are aware that `withdrawable` must be funded before a claim can succeed: [5](#0-4) 

**Exploit flow:**

1. Market created; lender supplies credit `C`; borrower takes debt `C`. State: `withdrawable = 0`, `continuousFeeCredit = 0`.
2. Time passes. Anyone calls `updatePosition(market, lender)`. State: `continuousFeeCredit = F > 0`, `withdrawable = 0` (unchanged).
3. Fee claimer calls `claimContinuousFee(market, F, receiver)`.
4. Line 320 executes `withdrawable -= F` → `0 - F` → underflow revert.

## Impact Explanation
The fee claimer cannot unilaterally extract any accrued continuous fees while borrower debt remains outstanding. In a market where all credit is borrowed and no repayments have occurred, `withdrawable` stays at 0 while `continuousFeeCredit` grows with every `updatePosition` call. Fees are not permanently lost — they become claimable after sufficient repayment — but the fee claimer is entirely dependent on borrower cooperation (repayment or liquidation) to access fees that have already been credited to them. This is a concrete, persistent denial of fee-claiming capability for the duration of any outstanding loan.

## Likelihood Explanation
The condition is reachable in any market with outstanding debt and no repayments. No attacker action is required: simply calling the permissionless `updatePosition` after time has elapsed is sufficient to create the state `continuousFeeCredit > 0, withdrawable = 0`. The condition persists for the entire duration that debt remains outstanding without repayment, which can span the full loan term. It is repeatable across all markets.

## Recommendation
In `_updatePosition`, increase `withdrawable` by `accruedFee` alongside the increase to `continuousFeeCredit`:

```solidity
marketState[id].continuousFeeCredit += UtilsLib.toUint128(accruedFee);
marketState[id].withdrawable        += UtilsLib.toUint128(accruedFee);
```

This maintains the invariant `continuousFeeCredit <= withdrawable` at all times and ensures the fee claimer can always extract fees that have been credited. The `withdrawableUnchanged` Certora rule would need to be updated to exclude `updatePosition`.

## Proof of Concept
Minimal Foundry test (no repay call):

```solidity
function testClaimBlockedWithoutRepay(uint256 credit, uint256 feeRate, uint256 ttm, uint256 elapsed) public {
    credit = bound(credit, 1, MAX_CREDIT);
    feeRate = bound(feeRate, 1, MAX_CONTINUOUS_FEE);
    ttm = bound(ttm, 2, 360 days);
    elapsed = bound(elapsed, 1, ttm - 1);

    setupLender(credit, feeRate, ttm);          // lender supplies, borrower takes all credit
    vm.warp(vm.getBlockTimestamp() + elapsed);
    midnight.updatePosition(market, lender);    // accrues fee → continuousFeeCredit > 0

    uint256 feeAmount = midnight.continuousFeeCredit(id);
    vm.assume(feeAmount > 0);
    assertEq(midnight.withdrawable(id), 0);     // withdrawable still 0

    vm.prank(feeClaimer);
    vm.expectRevert();                          // underflow on withdrawable -= feeAmount
    midnight.claimContinuousFee(market, feeAmount, feeClaimer);
}
```

### Citations

**File:** src/Midnight.sol (L318-320)
```text
        _marketState.continuousFeeCredit -= UtilsLib.toUint128(amount);
        _marketState.totalUnits -= UtilsLib.toUint128(amount);
        _marketState.withdrawable -= UtilsLib.toUint128(amount);
```

**File:** src/Midnight.sol (L842-846)
```text
        _position.credit = newCredit;
        _position.lastLossFactor = marketState[id].lossFactor;
        _position.pendingFee = newPendingFee;
        _position.lastAccrual = uint128(block.timestamp);
        marketState[id].continuousFeeCredit += UtilsLib.toUint128(accruedFee);
```

**File:** certora/specs/WithdrawableMonotonicity.spec (L11-27)
```text
rule repayIncreasesWithdrawable(env e, Midnight.Market market, uint256 units, address onBehalf, address callback, bytes data) {
    bytes32 id = toId(e, market);
    uint256 withdrawableBefore = withdrawable(id);
    repay(e, market, units, onBehalf, callback, data);
    uint256 withdrawableAfter = withdrawable(id);
    assert withdrawableAfter == withdrawableBefore + units;
}

rule liquidateIncreasesWithdrawable(env e, Midnight.Market market, uint256 collateralIndex, uint256 seizedAssets, uint256 repaidUnits, address borrower, address receiver, address callback, bytes data, bool postMaturityMode) {
    bytes32 id = toId(e, market);
    uint256 withdrawableBefore = withdrawable(id);
    uint256 seizedResult;
    uint256 repaidResult;
    seizedResult, repaidResult = liquidate(e, market, collateralIndex, seizedAssets, repaidUnits, borrower, postMaturityMode, receiver, callback, data);
    uint256 withdrawableAfter = withdrawable(id);
    assert withdrawableAfter == withdrawableBefore + repaidResult;
}
```

**File:** certora/specs/WithdrawableMonotonicity.spec (L45-57)
```text
rule withdrawableUnchanged(method f, env e, calldataarg args, bytes32 id)
filtered {
    f -> !f.isView
        && f.selector != sig:repay(Midnight.Market, uint256, address, address, bytes).selector
        && f.selector != sig:liquidate(Midnight.Market, uint256, uint256, uint256, address, bool, address, address, bytes).selector
        && f.selector != sig:withdraw(Midnight.Market, uint256, address, address).selector
        && f.selector != sig:claimContinuousFee(Midnight.Market, uint256, address).selector
} {
    uint256 withdrawableBefore = withdrawable(id);
    f(e, args);
    uint256 withdrawableAfter = withdrawable(id);
    assert withdrawableAfter == withdrawableBefore;
}
```

**File:** test/ContinuousFeeTest.sol (L420-423)
```text
        // Repay so withdrawable covers the claim.
        deal(address(loanToken), borrower, credit);
        vm.prank(borrower);
        midnight.repay(market, credit, borrower, address(0), hex"");
```
