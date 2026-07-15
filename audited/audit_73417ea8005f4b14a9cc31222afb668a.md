### Title
Paused Pool Does Not Block Withdrawals, Allowing Token Outflows During Emergency Pause - (File: src/ConfidencePool.sol)

### Summary
The `ConfidencePool` pause mechanism applies `whenPoolNotPaused` to `stake()` and `contributeBonus()` but not to `withdraw()`. When the pool owner pauses the pool to halt all operations, stakers in the pre-risk window can still call `withdraw()` and retrieve their stake, bypassing the intended emergency stop.

### Finding Description
`pause()` is an owner-controlled emergency stop backed by OpenZeppelin's `Pausable`. `stake()` and `contributeBonus()` both carry the `whenPoolNotPaused` modifier, blocking new token inflows. However, `withdraw()` has no `whenPoolNotPaused` guard. [1](#0-0) [2](#0-1) [3](#0-2) 

A staker whose `eligibleStake > 0` and who is in the pre-risk window (registry in `NOT_DEPLOYED`, `NEW_DEPLOYMENT`, or `ATTACK_REQUESTED`, and `riskWindowStart == 0`) can call `withdraw()` while the pool is paused and successfully retrieve their full stake. The pause therefore does not halt all token movements — only inflows. [4](#0-3) [5](#0-4) 

### Impact Explanation
The pool owner cannot use the pause mechanism to freeze all token movements. If the pause was triggered to protect against a vulnerability in the withdrawal path, or to preserve pool state during an investigation, stakers can still drain their positions. This undermines the emergency stop's effectiveness and could allow stakers to exit before a fix is applied. The `docs/DESIGN.md` does not document this asymmetry as intentional.

### Likelihood Explanation
The pre-risk window (where withdrawals are allowed) is the normal early lifecycle of every pool. A pool owner discovering a vulnerability and pausing the pool during this window is a realistic emergency scenario. Any staker with a nonzero `eligibleStake` can exploit this without any special privilege.

### Recommendation
Add `whenPoolNotPaused` to `withdraw()`:
```solidity
function withdraw() external nonReentrant whenPoolNotPaused {
```

### Proof of Concept
1. Pool is deployed; registry is in `NOT_DEPLOYED` / `NEW_DEPLOYMENT` / `ATTACK_REQUESTED`; `riskWindowStart == 0`.
2. Alice stakes tokens; `eligibleStake[Alice] > 0`.
3. Pool owner discovers a vulnerability and calls `pause()`.
4. Alice calls `withdraw()` — the call succeeds because `withdraw()` carries no `whenPoolNotPaused` guard.
5. Alice retrieves her full stake despite the pool being paused, bypassing the intended emergency stop. [6](#0-5) [7](#0-6)

### Citations

**File:** src/ConfidencePool.sol (L167-170)
```text
    modifier whenPoolNotPaused() {
        if (paused()) revert PoolPaused();
        _;
    }
```

**File:** src/ConfidencePool.sol (L222-222)
```text
    function stake(uint256 amount) external nonReentrant whenPoolNotPaused {
```

**File:** src/ConfidencePool.sol (L266-266)
```text
    function contributeBonus(uint256 amount) external nonReentrant whenPoolNotPaused {
```

**File:** src/ConfidencePool.sol (L288-319)
```text
    function withdraw() external nonReentrant {
        if (outcome != PoolStates.Outcome.UNRESOLVED) revert OutcomeAlreadySet();
        IAttackRegistry.ContractState state = _observePoolState();
        // `riskWindowStart` is the pool's one-way record that risk has materialised;
        // gate on it so an upstream registry rewind cannot re-open withdrawals.
        if (
            riskWindowStart != 0
                || (state != IAttackRegistry.ContractState.NOT_DEPLOYED
                    && state != IAttackRegistry.ContractState.NEW_DEPLOYMENT
                    && state != IAttackRegistry.ContractState.ATTACK_REQUESTED)
        ) {
            revert WithdrawsDisabled();
        }

        uint256 amount = eligibleStake[msg.sender];
        if (amount == 0) revert InvalidAmount();

        _clampUserSums(msg.sender);
        // Withdrawing forfeits the caller's bonus claim: subtract their full contribution from
        // the global accumulators so honest stakers' shares aren't diluted by the exiter's
        // forfeited weight.
        sumStakeTime -= userSumStakeTime[msg.sender];
        sumStakeTimeSq -= userSumStakeTimeSq[msg.sender];

        eligibleStake[msg.sender] = 0;
        userSumStakeTime[msg.sender] = 0;
        userSumStakeTimeSq[msg.sender] = 0;
        totalEligibleStake -= amount;

        stakeToken.safeTransfer(msg.sender, amount);
        emit Withdrawn(msg.sender, amount);
    }
```

**File:** src/ConfidencePool.sol (L662-664)
```text
    function pause() external onlyOwner whenPoolNotPaused {
        _pause();
    }
```
