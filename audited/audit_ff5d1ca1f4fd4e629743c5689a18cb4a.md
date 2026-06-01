### Title
Continuous fee claims permanently blocked when `continuousFeeCredit > 0` but `withdrawable == 0` - (File: `src/Midnight.sol`)

### Summary
`claimContinuousFee` unconditionally decrements `_marketState.withdrawable` by `amount`, which underflows and reverts whenever `withdrawable < amount`. Because `continuousFeeCredit` accumulates via `_updatePosition` inside `take` without any corresponding increase to `withdrawable`, the state `continuousFeeCredit > 0 && withdrawable == 0` is reachable in normal protocol operation, permanently blocking the fee claimer. The maker de-ratifying a root is not the root cause; the structural mismatch between the two counters exists regardless of ratifier state.

### Finding Description
**Code path – `claimContinuousFee`** (`src/Midnight.sol` lines 312–325):

```solidity
_marketState.continuousFeeCredit -= UtilsLib.toUint128(amount);
_marketState.totalUnits          -= UtilsLib.toUint128(amount);
_marketState.withdrawable        -= UtilsLib.toUint128(amount);  // reverts if withdrawable < amount
```

There is no explicit guard; the revert comes from Solidity 0.8 checked arithmetic on the last line. [1](#0-0) 

**How `continuousFeeCredit` grows without `withdrawable` growing:**

`_updatePosition` (called inside `take` at lines 379–380) increases `continuousFeeCredit` by the accrued fee but leaves `withdrawable` untouched: [2](#0-1) [3](#0-2) 

`withdrawable` is increased **only** by `repay` (line 509) and `liquidate` (line 675): [4](#0-3) [5](#0-4) 

**Exploit flow (no malicious action required):**

1. Market is created; lender posts a buy offer via `SetterRatifier`.
2. Maker ratifies root; taker calls `take` → buyer gets credit, `_updatePosition` is triggered, `continuousFeeCredit` starts accumulating.
3. Time passes; `updatePosition` is called (permissionless) → `continuousFeeCredit` grows further; `withdrawable` remains 0.
4. No borrower has called `repay`; no liquidation has occurred.
5. `feeClaimer` calls `claimContinuousFee(market, 1, receiver)` → reverts because `withdrawable == 0`.

The maker calling `setIsRootRatified(maker, root, false)` prevents future takes, but this is irrelevant: `repay` and `liquidate` (the only functions that raise `withdrawable`) require no ratification and are unaffected by ratifier state. The freeze is caused by the accounting mismatch, not by the ratifier. [6](#0-5) 

The Certora liveness rule for `claimContinuousFee` independently confirms the constraint: success requires `amount <= withdrawableBefore` in addition to `amount <= continuousFeeCreditBefore`: [7](#0-6) 

The existing test `testClaimContinuousFee` works around this by explicitly repaying the full debt before claiming, acknowledging that `withdrawable` must cover the claim: [8](#0-7) 

### Impact Explanation
The fee claimer cannot claim any accrued continuous fees while `withdrawable == 0`, even when `continuousFeeCredit` is large. Because `continuousFeeCredit` accumulates from lenders' credit reductions (lenders have already paid), the protocol holds tokens it cannot distribute to the fee claimer. In markets where borrowers are slow to repay or are underwater (waiting for liquidation), this freeze can persist indefinitely. If bad debt is eventually realized, `continuousFeeCredit` is slashed proportionally, so the protocol may permanently lose the ability to pay out fees that were legitimately accrued.

### Likelihood Explanation
This is a natural protocol state, not an edge case. Any market where takes have occurred but no repayments or liquidations have yet happened will exhibit `continuousFeeCredit > 0 && withdrawable == 0`. No privileged or malicious actor is required. The freeze lasts until at least one repayment or liquidation occurs, which may be delayed by market conditions, borrower behavior, or oracle staleness.

### Recommendation
Decouple `continuousFeeCredit` from `withdrawable` in `claimContinuousFee`. The fee claimer should be able to claim up to `min(continuousFeeCredit, totalUnits)` without requiring `withdrawable >= amount`. One concrete fix: remove the `withdrawable` decrement from `claimContinuousFee` and instead track a separate `feeWithdrawable` counter that is incremented by `_updatePosition` alongside `continuousFeeCredit`, ensuring the contract always holds sufficient tokens for fee claims independently of borrower repayment timing.

### Proof of Concept
```solidity
function testClaimContinuousFeeBlockedWithoutRepay(
    uint256 credit, uint256 feeRate, uint256 ttm, uint256 elapsed
) public {
    credit = bound(credit, 1e18, 1e24);
    feeRate = bound(feeRate, 1, MAX_CONTINUOUS_FEE);
    ttm = bound(ttm, 2, 360 days);
    elapsed = bound(elapsed, 1, ttm - 1);

    // Step 1: lender takes offer → buyer gets credit
    setupLender(credit, feeRate, ttm);

    // Step 2: time passes, fees accrue to continuousFeeCredit
    vm.warp(vm.getBlockTimestamp() + elapsed);
    midnight.updatePosition(market, lender);

    uint256 feeCredit = midnight.continuousFeeCredit(id);
    uint256 wdrawable = midnight.withdrawable(id);

    // Step 3: assert the problematic state
    vm.assume(feeCredit > 0);
    assertEq(wdrawable, 0, "withdrawable is zero: no repayments");

    // Step 4: feeClaimer attempts to claim → must revert
    vm.prank(feeClaimer);
    vm.expectRevert(); // arithmetic underflow on withdrawable -= amount
    midnight.claimContinuousFee(market, 1, feeClaimer);
}
```

Expected assertions:
- `continuousFeeCredit > 0` after `updatePosition`
- `withdrawable == 0` (no repayments)
- `claimContinuousFee` reverts with arithmetic underflow on `_marketState.withdrawable -= 1`

### Citations

**File:** src/Midnight.sol (L318-320)
```text
        _marketState.continuousFeeCredit -= UtilsLib.toUint128(amount);
        _marketState.totalUnits -= UtilsLib.toUint128(amount);
        _marketState.withdrawable -= UtilsLib.toUint128(amount);
```

**File:** src/Midnight.sol (L379-380)
```text
        if (hasCredit(id, buyer) || units > buyerPos.debt) _updatePosition(offer.market, id, buyer);
        if (hasCredit(id, seller)) _updatePosition(offer.market, id, seller);
```

**File:** src/Midnight.sol (L509-509)
```text
        marketState[id].withdrawable += UtilsLib.toUint128(units);
```

**File:** src/Midnight.sol (L675-675)
```text
            _marketState.withdrawable += UtilsLib.toUint128(repaidUnits);
```

**File:** src/Midnight.sol (L846-846)
```text
        marketState[id].continuousFeeCredit += UtilsLib.toUint128(accruedFee);
```

**File:** src/ratifiers/SetterRatifier.sol (L24-28)
```text
    function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootRatified[maker][root] = newIsRootRatified;
        emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
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
