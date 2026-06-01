### Title
Bad-debt liquidation front-run reduces `continuousFeeCredit` below feeClaimer's claim amount, causing arithmetic underflow revert - (File: src/libraries/EventsLib.sol → `ClaimContinuousFee` / `src/Midnight.sol` → `claimContinuousFee`)

### Summary
`claimContinuousFee` performs an unchecked subtraction `_marketState.continuousFeeCredit -= UtilsLib.toUint128(amount)` with no guard ensuring `amount` still fits within the current `continuousFeeCredit` at execution time. A bad-debt liquidation atomically scales `continuousFeeCredit` down by the ratio `(type(uint128).max - newLossFactor) / (type(uint128).max - oldLossFactor)`, which is strictly less than 1. An unprivileged liquidator can front-run the feeClaimer's transaction with such a liquidation, reducing `continuousFeeCredit` below the claimed amount and causing the feeClaimer's transaction to revert with an arithmetic underflow.

### Finding Description

**Code path — `claimContinuousFee` (`src/Midnight.sol:312-325`):**

```solidity
function claimContinuousFee(Market memory market, uint256 amount, address receiver) external {
    bytes32 id = toId(market);
    MarketState storage _marketState = marketState[id];
    require(msg.sender == feeClaimer, OnlyFeeClaimer());
    require(_marketState.tickSpacing > 0, MarketNotCreated());

    _marketState.continuousFeeCredit -= UtilsLib.toUint128(amount);   // ← underflows if amount > continuousFeeCredit
    ...
}
```

There is no `require(amount <= _marketState.continuousFeeCredit)` guard. The subtraction is a checked uint128 operation that reverts on underflow (Solidity ≥0.8).

**Code path — bad-debt realization in `liquidate` (`src/Midnight.sol:626-641`):**

```solidity
if (badDebt > 0) {
    ...
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

Because `newLossFactor > oldLossFactor`, the multiplier `(type(uint128).max - newLossFactor) / (type(uint128).max - oldLossFactor)` is strictly less than 1, so `continuousFeeCredit` is strictly reduced.

**Exploit flow:**

1. `continuousFeeCredit` = X (accumulated via `_updatePosition` calls).
2. feeClaimer reads X off-chain and broadcasts `claimContinuousFee(market, X, receiver)`.
3. Attacker (liquidator) observes the pending transaction in the mempool and front-runs it with `liquidate(market, ..., borrower, ...)` on a position with bad debt, reducing `continuousFeeCredit` to X′ < X.
4. feeClaimer's transaction executes: `continuousFeeCredit -= X` → X′ - X underflows → revert.

**Why existing checks fail:**

- `claimContinuousFee` only checks `msg.sender == feeClaimer` and `tickSpacing > 0`. There is no snapshot or minimum-amount guard.
- The Certora spec (`certora/specs/Role.spec:264`) formally states the claim succeeds iff `amount <= continuousFeeCreditBefore`, but this is verified in isolation — it does not protect against the race condition between the feeClaimer reading the value and the transaction landing.
- `liquidatorGate` is market-specific and optional (`address(0)` by default), so it does not block the attacker in the general case.

### Impact Explanation
The feeClaimer is permanently unable to claim the full accrued `continuousFeeCredit` whenever a bad-debt liquidation can be front-run. The attacker can repeat this every time the feeClaimer attempts to claim, causing an indefinite DoS against fee collection. Protocol revenue is locked in the contract and cannot be extracted.

### Likelihood Explanation
**Preconditions:**
- A borrower position with bad debt must exist (or be creatable) in the target market.
- The feeClaimer must attempt to claim an amount equal to the current `continuousFeeCredit` (the natural behavior when sweeping all accrued fees).
- The attacker must be able to observe the feeClaimer's pending transaction (public mempool on most EVM chains).

**Feasibility:** High. Bad-debt positions are a normal protocol event. Any liquidator (unprivileged) can call `liquidate` with `seizedAssets=0, repaidUnits=0` to realize bad debt at zero token cost (only gas). The attack is repeatable as long as any bad-debt position exists in the market. The attacker's cost is only gas for the liquidation.

### Recommendation
Add an explicit guard in `claimContinuousFee` that caps the claim to the current `continuousFeeCredit`, or use a saturating subtraction:

```solidity
uint128 credit = _marketState.continuousFeeCredit;
require(UtilsLib.toUint128(amount) <= credit, ClaimExceedsContinuousFeeCredit());
_marketState.continuousFeeCredit = credit - UtilsLib.toUint128(amount);
```

Alternatively, accept a `minAmount` parameter so the feeClaimer can express a slippage tolerance and the transaction reverts only when the received amount is below an acceptable threshold, rather than always reverting on any reduction.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

// Foundry fuzz test
function testFuzz_claimContinuousFeeDoSViaBadDebtFrontRun(
    uint256 credit,
    uint256 feeRate,
    uint256 ttm,
    uint256 elapsed,
    uint256 badDebtFraction  // fraction of totalUnits to wipe as bad debt
) public {
    credit = bound(credit, 1e18, MAX_CREDIT);
    feeRate = bound(feeRate, 1, MAX_CONTINUOUS_FEE);
    ttm = bound(ttm, 10, 360 days);
    elapsed = bound(elapsed, 1, ttm - 1);

    // 1. Setup: lender provides credit, borrower borrows
    setupLender(credit, feeRate, ttm);
    vm.warp(vm.getBlockTimestamp() + elapsed);

    // 2. Accrue continuous fee
    midnight.updatePosition(market, lender);
    uint256 feeCredit = midnight.continuousFeeCredit(id);
    vm.assume(feeCredit > 0);

    // 3. feeClaimer reads feeCredit and prepares to claim the full amount
    uint256 claimAmount = feeCredit;

    // 4. Attacker front-runs: trigger bad-debt liquidation (seizedAssets=0, repaidUnits=0)
    Oracle(market.collateralParams[0].oracle).setPrice(0); // force bad debt
    midnight.liquidate(market, 0, 0, 0, borrower, false, address(this), address(0), "");

    // 5. continuousFeeCredit is now reduced below claimAmount
    uint256 feeCreditAfterBadDebt = midnight.continuousFeeCredit(id);
    assertLt(feeCreditAfterBadDebt, claimAmount, "bad debt reduced continuousFeeCredit");

    // 6. feeClaimer's transaction now reverts with arithmetic underflow
    vm.prank(feeClaimer);
    vm.expectRevert(); // arithmetic underflow: continuousFeeCredit - claimAmount underflows
    midnight.claimContinuousFee(market, claimAmount, feeClaimer);
}
```

**Expected assertion:** The `vm.expectRevert()` passes, confirming the feeClaimer's claim reverts due to underflow at `_marketState.continuousFeeCredit -= UtilsLib.toUint128(amount)` after the bad-debt liquidation reduces `continuousFeeCredit` below `claimAmount`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** src/libraries/EventsLib.sol (L31-31)
```text
    event ClaimContinuousFee(address indexed caller, bytes32 indexed id_, uint256 amount, address indexed receiver);
```

**File:** certora/specs/Role.spec (L251-271)
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
    assert !reverted => withdrawable(id) == withdrawableBefore - amount;
    assert !reverted => totalUnits(id) == totalUnitsBefore - amount;
    assert !reverted => currentContract.marketState[id].continuousFeeCredit == continuousFeeCreditBefore - amount;
    assert !reverted => tokenBalance[market.loanToken][currentContract] == midnightBalanceBefore - (receiver == currentContract ? 0 : amount);
    assert !reverted => tokenBalance[market.loanToken][receiver] == receiverBalanceBefore + (receiver == currentContract ? 0 : amount);
    assert !reverted => user != currentContract && user != receiver => tokenBalance[market.loanToken][user] == userBalanceBefore;
}
```
