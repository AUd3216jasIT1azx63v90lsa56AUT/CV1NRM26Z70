### Title
`withdraw()` Missing `whenPoolNotPaused` Modifier Allows Stakers to Bypass Emergency Pause - (File: src/ConfidencePool.sol)

### Summary
`withdraw()` in `ConfidencePool.sol` lacks the `whenPoolNotPaused` modifier that both `stake()` and `contributeBonus()` carry. When the pool owner pauses the contract, stakers can still call `withdraw()` directly to retrieve their principal, bypassing the intended pause protection entirely.

### Finding Description
`stake()` and `contributeBonus()` are both declared with `whenPoolNotPaused`:

```solidity
// line 222
function stake(uint256 amount) external nonReentrant whenPoolNotPaused { ... }

// line 266
function contributeBonus(uint256 amount) external nonReentrant whenPoolNotPaused { ... }
```

But `withdraw()` carries no such guard:

```solidity
// line 288
function withdraw() external nonReentrant {
    if (outcome != PoolStates.Outcome.UNRESOLVED) revert OutcomeAlreadySet();
    IAttackRegistry.ContractState state = _observePoolState();
    if (
        riskWindowStart != 0
            || (state != IAttackRegistry.ContractState.NOT_DEPLOYED
                && state != IAttackRegistry.ContractState.NEW_DEPLOYMENT
                && state != IAttackRegistry.ContractState.ATTACK_REQUESTED)
    ) {
        revert WithdrawsDisabled();
    }
    ...
    stakeToken.safeTransfer(msg.sender, amount);
}
```

The only guards on `withdraw()` are the registry-state latch (`riskWindowStart != 0`) and the outcome check — neither of which is affected by the paused flag. `docs/DESIGN.md §9` documents the registry-state gating for `withdraw()` but makes no mention of the pause being intentionally absent from it. This is an implementation defect, not a documented design decision.

### Impact Explanation
When the pool owner pauses the pool — the expected emergency-halt mechanism — any staker can still call `withdraw()` and receive their full `eligibleStake` back. The pause fails to halt token outflows. If the pause was triggered precisely because a bug in the withdrawal accounting was discovered (e.g., incorrect `sumStakeTime` / `sumStakeTimeSq` decrements), stakers can continue draining the pool through the unguarded path, defeating the purpose of the emergency stop. Additionally, each successful `withdraw()` during a pause mutates `totalEligibleStake`, `sumStakeTime`, and `sumStakeTimeSq`, potentially corrupting the global accounting state that the owner was trying to freeze.

### Likelihood Explanation
The pool owner must first call `pause()`. Once paused, any staker with a nonzero `eligibleStake` in a pre-risk registry state can immediately exploit this — no special knowledge or tooling required, just a direct call to `withdraw()`. The precondition (pool is paused) is an honest operational action, not an adversarial one, making the bypass trivially reachable by any unprivileged staker.

### Recommendation
Add `whenPoolNotPaused` to `withdraw()`, consistent with `stake()` and `contributeBonus()`:

```solidity
function withdraw() external nonReentrant whenPoolNotPaused {
```

This ensures the pause mechanism uniformly halts all fund-moving operations, matching the documented intent of the pause/unpause owner controls.

### Proof of Concept
1. Pool is in a pre-risk state (`NOT_DEPLOYED` / `NEW_DEPLOYMENT` / `ATTACK_REQUESTED`); Alice has staked 1000 tokens.
2. Owner discovers an accounting bug and calls `pause()`.
3. Alice calls `stake()` → reverts with `PoolPaused` (pause works here).
4. Alice calls `withdraw()` → **succeeds**, transfers 1000 tokens to Alice, and decrements `totalEligibleStake`, `sumStakeTime`, `sumStakeTimeSq` — all while the pool is supposed to be frozen.
5. The owner's emergency pause has been bypassed by an unprivileged caller with zero special access. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

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

**File:** docs/DESIGN.md (L235-250)
```markdown
## 9. Withdraw lifecycle

`withdraw()` exits the caller's full eligible stake in one call. It is gated by registry state:
allowed in `NOT_DEPLOYED`, `NEW_DEPLOYMENT`, and `ATTACK_REQUESTED`; permanently disabled from
`UNDER_ATTACK` onward (gated additionally on the one-way `riskWindowStart != 0` latch, so an
upstream registry rewind cannot re-open it).

This closes the race in which a staker observes an attack on-chain and front-runs `flagOutcome`
with `withdraw()` to escape with full value. Withdrawing before a flag forfeits any bonus the
caller would have earned (the claim paths require nonzero `eligibleStake`).

The withdraw escape hatch (open for the entire pre-attack window) is also why "no observed risk →
no bonus" is fair: a staker only forfeits the exit option once risk has actually materialized,
which is exactly when they begin earning the risk premium. A sponsor cannot grief stakers by
keeping the agreement out of attackable mode — stakers can freely exit until risk materializes.

```
