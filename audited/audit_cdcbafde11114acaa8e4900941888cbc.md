### Title
Continuous Fee Unclaimable When `withdrawable < continuousFeeCredit` Due to Missing `withdrawable` Increase in `_updatePosition` - (File: src/Midnight.sol)

### Summary
`_updatePosition` increases `continuousFeeCredit` by the accrued fee but never increases `withdrawable`. Since `claimContinuousFee` decrements all three of `continuousFeeCredit`, `totalUnits`, and `withdrawable` atomically, it will revert via underflow on line 320 whenever `amount > withdrawable`, even if `amount <= continuousFeeCredit`. The fee claimer is therefore blocked from extracting any accrued continuous fees until borrowers repay enough to cover the claim amount.

### Finding Description

**Root cause — `_updatePosition` does not touch `withdrawable`:**

`_updatePosition` at `src/Midnight.sol:832-851` reduces the lender's `credit` by `accruedFee` and credits that amount to `marketState[id].continuousFeeCredit`:

```solidity
_position.credit = newCredit;          // credit -= accruedFee
marketState[id].continuousFeeCredit += UtilsLib.toUint128(accruedFee);
``` [1](#0-0) 

`withdrawable` is not touched. The only functions that increase `withdrawable` are `repay` and `liquidate`: [2](#0-1) [3](#0-2) 

This is confirmed by the Certora spec `withdrawableUnchanged`, which excludes only `repay`, `liquidate`, `withdraw`, and `claimContinuousFee` from the "unchanged" rule — `updatePosition` is not excluded, confirming it leaves `withdrawable` unchanged: [4](#0-3) 

**Revert path in `claimContinuousFee`:**

`claimContinuousFee` at `src/Midnight.sol:312-325` performs three unchecked decrements in sequence:

```solidity
_marketState.continuousFeeCredit -= UtilsLib.toUint128(amount);
_marketState.totalUnits          -= UtilsLib.toUint128(amount);
_marketState.withdrawable        -= UtilsLib.toUint128(amount);  // ← underflow revert
``` [5](#0-4) 

There is no explicit `require(amount <= withdrawable)` guard; the revert is a Solidity checked-arithmetic underflow. The Certora liveness rule for `claimContinuousFee` explicitly documents that success requires **both** `amount <= withdrawable` **and** `amount <= continuousFeeCredit`: [6](#0-5) 

**Exploit flow:**

1. Market created, lender supplies credit `C`, borrower takes debt `C`. State: `withdrawable = 0`, `continuousFeeCredit = 0`, `totalUnits = C`.
2. Time passes. Anyone calls `updatePosition(market, lender)`. State: `continuousFeeCredit = F > 0`, `withdrawable = 0` (unchanged).
3. Fee claimer calls `claimContinuousFee(market, F, receiver)`.
4. Line 320 executes `withdrawable -= F` → `0 - F` → underflow revert.

The existing test `testClaimContinuousFee` in `test/ContinuousFeeTest.sol` explicitly works around this by calling `repay` first, with the comment *"Repay so withdrawable covers the claim"*: [7](#0-6) 

This confirms the protocol authors are aware of the dependency but have not enforced the invariant `continuousFeeCredit <= withdrawable` at all times.

### Impact Explanation

The continuous fee is unclaimable whenever `withdrawable < continuousFeeCredit`. In a market where all debt is outstanding and no repayments have occurred, `withdrawable` remains 0 while `continuousFeeCredit` grows with every `updatePosition` call. The fee claimer cannot extract any accrued fees without borrower cooperation (repayment or liquidation). Fees are not lost permanently — they become claimable after sufficient repayment — but the fee claimer has no unilateral ability to claim fees that have already been accrued and credited.

### Likelihood Explanation

This condition is reachable in any market with outstanding debt and no repayments. It requires no special attacker action: simply calling `updatePosition` (which anyone can do) after time has elapsed is sufficient to create the state `continuousFeeCredit > 0, withdrawable = 0`. The condition persists for the entire duration that debt remains outstanding without repayment, which can be the full loan term. It is repeatable across all markets.

### Recommendation

When `_updatePosition` accrues a fee by reducing a lender's credit, the corresponding tokens are still locked as borrower debt — they are not yet in the contract. `claimContinuousFee` should not decrement `withdrawable` at all, or alternatively `_updatePosition` should increase `withdrawable` by `accruedFee` to reflect that the fee has been "pre-claimed" from the lender's credit. The cleaner fix is to track `continuousFeeCredit` independently of `withdrawable`: remove the `_marketState.withdrawable -= amount` line from `claimContinuousFee` and instead transfer tokens from a separate fee reserve that is funded when fees are accrued (i.e., increase `withdrawable` by `accruedFee` in `_updatePosition` to represent the protocol's share of the lender's credit being converted to a claimable balance).

### Proof of Concept

```solidity
function testClaimContinuousFeeRevertsWithNoRepay() public {
    uint256 credit = 1e18;
    uint256 feeRate = 1e14; // non-zero continuous fee
    uint256 ttm = 30 days;

    // Setup: lender supplies credit, borrower takes debt
    setupLender(credit, feeRate, ttm);
    // State: withdrawable == 0, continuousFeeCredit == 0

    // Time passes, fee accrues
    vm.warp(block.timestamp + 15 days);
    midnight.updatePosition(market, lender);

    uint256 feeCredit = midnight.continuousFeeCredit(id);
    uint256 wdrawable = midnight.withdrawable(id);

    // Assert preconditions
    assertGt(feeCredit, 0, "continuousFeeCredit must be > 0");
    assertEq(wdrawable, 0, "withdrawable must be 0 (no repayments)");

    // Fee claimer attempts to claim — must revert (underflow on withdrawable)
    vm.prank(feeClaimer);
    vm.expectRevert(); // arithmetic underflow
    midnight.claimContinuousFee(market, feeCredit, feeClaimer);
}
```

Expected assertion: `claimContinuousFee` reverts with an arithmetic underflow on `_marketState.withdrawable -= amount` at `src/Midnight.sol:320`, even though `amount <= continuousFeeCredit`.

### Citations

**File:** src/Midnight.sol (L318-320)
```text
        _marketState.continuousFeeCredit -= UtilsLib.toUint128(amount);
        _marketState.totalUnits -= UtilsLib.toUint128(amount);
        _marketState.withdrawable -= UtilsLib.toUint128(amount);
```

**File:** src/Midnight.sol (L508-509)
```text
        position[id][onBehalf].debt -= UtilsLib.toUint128(units);
        marketState[id].withdrawable += UtilsLib.toUint128(units);
```

**File:** src/Midnight.sol (L675-675)
```text
            _marketState.withdrawable += UtilsLib.toUint128(repaidUnits);
```

**File:** src/Midnight.sol (L842-846)
```text
        _position.credit = newCredit;
        _position.lastLossFactor = marketState[id].lossFactor;
        _position.pendingFee = newPendingFee;
        _position.lastAccrual = uint128(block.timestamp);
        marketState[id].continuousFeeCredit += UtilsLib.toUint128(accruedFee);
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

**File:** certora/specs/Role.spec (L264-264)
```text
    assert !reverted <=> e.msg.sender == feeClaimerBefore && e.msg.value == 0 && marketIsCreated && amount <= withdrawableBefore && amount <= totalUnitsBefore && amount <= continuousFeeCreditBefore;
```

**File:** test/ContinuousFeeTest.sol (L420-423)
```text
        // Repay so withdrawable covers the claim.
        deal(address(loanToken), borrower, credit);
        vm.prank(borrower);
        midnight.repay(market, credit, borrower, address(0), hex"");
```
