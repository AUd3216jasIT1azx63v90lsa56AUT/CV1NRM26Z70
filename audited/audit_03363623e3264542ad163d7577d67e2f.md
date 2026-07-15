### Title
`withdraw()` Missing `whenPoolNotPaused` Modifier Allows Stake Withdrawal During Pause - (File: src/ConfidencePool.sol)

### Summary
`withdraw()` in `ConfidencePool.sol` lacks the `whenPoolNotPaused` modifier that is explicitly applied to both `stake()` and `contributeBonus()`. When the pool owner pauses the contract in response to an incident, stakers can still drain their principal, defeating the purpose of the pause mechanism.

### Finding Description
`stake()` and `contributeBonus()` both carry the `whenPoolNotPaused` guard:

```solidity
function stake(uint256 amount) external nonReentrant whenPoolNotPaused {
function contributeBonus(uint256 amount) external nonReentrant whenPoolNotPaused {
```

`withdraw()` does not:

```solidity
function withdraw() external nonReentrant {
``` [1](#0-0) [2](#0-1) [3](#0-2) 

The `whenPoolNotPaused` modifier is defined at lines 167–170 and reverts with `PoolPaused` when `paused()` is true: [4](#0-3) 

`docs/DESIGN.md` §9 documents the withdraw lifecycle exclusively in terms of registry-state gating (`riskWindowStart != 0` latch, `NOT_DEPLOYED`/`NEW_DEPLOYMENT`/`ATTACK_REQUESTED` allowed). It makes no mention of pause-state gating for `withdraw()`, confirming this is not a documented intentional asymmetry. [5](#0-4) 

### Impact Explanation
The owner pauses the pool to freeze fund movements during an incident (e.g., a discovered implementation defect). Because `withdraw()` ignores the paused state, every staker with nonzero `eligibleStake` can immediately call `withdraw()` and recover their principal. This drains the pool's stake balance while the owner believes operations are frozen, eliminating the protective value of the pause mechanism for the pre-risk window where withdrawals are otherwise permitted.

### Likelihood Explanation
The owner must first invoke `pause()` (a trusted, honest action). Once paused, the path is fully permissionless: any staker calls `withdraw()` with no additional preconditions beyond having nonzero `eligibleStake` and the pool being in `UNRESOLVED` state with `riskWindowStart == 0`. No special privileges, no timing constraints beyond the registry state already required by the withdraw gate itself.

### Recommendation
Add `whenPoolNotPaused` to `withdraw()`, consistent with `stake()` and `contributeBonus()`:

```solidity
function withdraw() external nonReentrant whenPoolNotPaused {
```

### Proof of Concept
1. Pool is deployed and in `UNRESOLVED` state; registry is in `NEW_DEPLOYMENT` (withdrawals normally permitted).
2. Alice stakes via `stake(amount)` — succeeds.
3. Owner discovers an issue and calls `pause()` — pool is now paused.
4. Alice calls `withdraw()` — **succeeds** despite the pool being paused, because `withdraw()` has no `whenPoolNotPaused` guard.
5. Alice receives her full `eligibleStake` back; the pool's stake balance is reduced.
6. The owner's pause has not frozen fund outflows as intended. [6](#0-5)

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

**File:** docs/DESIGN.md (L237-249)
```markdown
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
