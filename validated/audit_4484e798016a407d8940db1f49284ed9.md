Analyzing the external report's vulnerability class — no deadline/slippage check on a state-changing function that depends on external state that can change between tx submission and confirmation — and mapping it to `ConfidencePool.sol`.

The analog is `stake()`: it reads live registry state via `_observePoolState()` at confirmation time, but accepts no deadline parameter. A staker who submits during `NEW_DEPLOYMENT` (pre-risk, withdrawals open) can have their tx confirmed after the registry transitions to `UNDER_ATTACK`, permanently locking them in without consent.

Key code paths confirm the defect:
- `stake()` calls `_observePoolState()` → `_markRiskWindowStart()` fires if state is `UNDER_ATTACK`, setting `riskWindowStart` [1](#0-0) 
- `_assertDepositsAllowed()` allows `UNDER_ATTACK` [2](#0-1) 
- `withdraw()` permanently reverts when `riskWindowStart != 0` [3](#0-2) 
- Entry time is floored at `riskWindowStart`, crushing bonus to near-zero [4](#0-3) 

DESIGN.md §3 says "A staker who does not want this can read the live registry state before staking" — but without a deadline parameter, reading state before submission provides no protection against the race condition. The design implies the staker can protect themselves; the implementation provides no mechanism to enforce it. DESIGN.md §3's "not a trap" argument applies to *intentional* staking during `UNDER_ATTACK` (staker sees the state, accepts the risk voluntarily). The race condition case is distinct: the staker submitted during `NEW_DEPLOYMENT` and never consented to the `UNDER_ATTACK` commitment.

---

### Title
No deadline parameter on `stake()` allows race condition to lock stakers into UNDER_ATTACK without consent - (File: src/ConfidencePool.sol)

### Summary
`stake()` reads live registry state at confirmation time but accepts no deadline parameter. A staker who submits during a pre-risk state (`NEW_DEPLOYMENT`, `ATTACK_REQUESTED`) can have their transaction confirmed after the registry transitions to `UNDER_ATTACK`, permanently losing withdrawal rights and earning near-zero bonus — contrary to their intent at submission time.

### Finding Description
`stake()` calls `_observePoolState()` which reads the live registry state at the block of confirmation, not at the block of submission. [5](#0-4) 

If the registry transitions from `NEW_DEPLOYMENT` to `UNDER_ATTACK` while the staker's transaction is pending:

1. `_assertDepositsAllowed()` passes — `UNDER_ATTACK` is explicitly allowed [2](#0-1) 
2. `_observePoolState()` triggers `_markRiskWindowStart()`, setting `riskWindowStart = block.timestamp` [1](#0-0) 
3. The staker's entry time is floored at `riskWindowStart`, so `(T − entry)² ≈ 0` — near-zero bonus [4](#0-3) 
4. `withdraw()` is permanently disabled because `riskWindowStart != 0` [3](#0-2) 

DESIGN.md §3 states "A staker who does not want this can read the live registry state before staking," implying the staker can protect themselves. But `stake()` has no `deadline` parameter, so reading the state before submission provides no on-chain enforcement against the race condition. The design's "voluntary risk" framing applies to stakers who *intentionally* stake during `UNDER_ATTACK` with full knowledge; it does not cover a staker whose transaction was submitted during `NEW_DEPLOYMENT` and confirmed during `UNDER_ATTACK` without their consent.

### Impact Explanation
A staker caught by this race condition:
- **Permanently loses withdrawal rights** — they submitted expecting to be able to exit pre-risk, but `riskWindowStart != 0` latches `withdraw()` closed forever [3](#0-2) 
- **Earns near-zero bonus** — entry floored at `riskWindowStart` collapses their k=2 score to approximately zero [6](#0-5) 
- **Risks principal loss** — if the pool resolves `CORRUPTED` (bad-faith), their principal sweeps to `recoveryAddress` with no recourse, despite never intending to accept that risk

### Likelihood Explanation
Requires the registry to transition to `UNDER_ATTACK` while the staker's transaction is pending. On BattleChain (EVM-compatible L2 with short block times), the race window per block is narrow but non-zero. The transition is an external protocol event that can occur at any time during the pool's pre-risk phase. The staker has no on-chain mechanism to prevent it without a deadline parameter.

### Recommendation
Add a `deadline` parameter to `stake()` that reverts if `block.timestamp > deadline`:

```solidity
function stake(uint256 amount, uint256 deadline) external nonReentrant whenPoolNotPaused {
    if (block.timestamp > deadline) revert DeadlineExpired();
    // ... rest of function
}
```

Optionally, add a `maxState` parameter (e.g., `IAttackRegistry.ContractState maxState`) that reverts if the observed registry state exceeds the staker's expectation, providing slippage-style protection analogous to `minOut` in AMM swaps.

### Proof of Concept
1. Registry is in `NEW_DEPLOYMENT`. Alice reads the state off-chain and decides to stake, expecting to be able to `withdraw()` if the agreement deteriorates.
2. Alice submits `stake(1000)` with no deadline protection.
3. Before Alice's tx confirms, the registry transitions to `UNDER_ATTACK` (a separate tx by the protocol).
4. Alice's `stake()` confirms: `_observePoolState()` observes `UNDER_ATTACK`, `_markRiskWindowStart()` fires, `riskWindowStart` is set to `block.timestamp`. Alice's `sumStakeTime` and `sumStakeTimeSq` are recorded at `riskWindowStart`. [7](#0-6) 
5. Alice calls `withdraw()` — reverts `WithdrawsDisabled` because `riskWindowStart != 0`. [3](#0-2) 
6. At resolution, Alice's bonus share is `(T − riskWindowStart)² × stake / globalScore ≈ 0` since her entry equals `riskWindowStart`. [8](#0-7) 
7. Alice is locked in with near-zero bonus and no exit path, contrary to her intent when she submitted the transaction.

### Citations

**File:** src/ConfidencePool.sol (L222-227)
```text
    function stake(uint256 amount) external nonReentrant whenPoolNotPaused {
        if (amount == 0) revert InvalidAmount();
        if (amount < minStake) revert BelowMinStake();
        if (outcome != PoolStates.Outcome.UNRESOLVED) revert OutcomeAlreadySet();
        if (block.timestamp >= expiry) revert StakingClosed();
        _assertDepositsAllowed(_observePoolState());
```

**File:** src/ConfidencePool.sol (L248-253)
```text
        uint256 newEntry = block.timestamp;
        uint256 start = riskWindowStart;
        if (start != 0 && newEntry < start) newEntry = start;

        uint256 contribTime = received * newEntry;
        uint256 contribTimeSq = received * newEntry * newEntry;
```

**File:** src/ConfidencePool.sol (L293-299)
```text
        if (
            riskWindowStart != 0
                || (state != IAttackRegistry.ContractState.NOT_DEPLOYED
                    && state != IAttackRegistry.ContractState.NEW_DEPLOYMENT
                    && state != IAttackRegistry.ContractState.ATTACK_REQUESTED)
        ) {
            revert WithdrawsDisabled();
```

**File:** src/ConfidencePool.sol (L677-684)
```text
    function _clampUserSums(address u) internal {
        uint256 start = riskWindowStart;
        uint256 stake_ = eligibleStake[u];
        if (start == 0 || stake_ == 0) return;
        if (userSumStakeTime[u] < stake_ * start) {
            userSumStakeTime[u] = stake_ * start;
            userSumStakeTimeSq[u] = stake_ * start * start;
        }
```

**File:** src/ConfidencePool.sol (L700-706)
```text
        uint256 T = outcomeFlaggedAt;

        // Underflow guards on both subtractions: globally the sum of squares is nonneg, but
        // truncation/rounding pathologies could push individual terms over.
        uint256 userPlus = T * T * userEligible + userSumStakeTimeSq[u];
        uint256 userMinus = 2 * T * userSumStakeTime[u];
        uint256 userScore = userPlus > userMinus ? userPlus - userMinus : 0;
```

**File:** src/ConfidencePool.sol (L727-734)
```text
    function _assertDepositsAllowed(IAttackRegistry.ContractState state) private pure {
        if (
            state == IAttackRegistry.ContractState.PROMOTION_REQUESTED
                || state == IAttackRegistry.ContractState.PRODUCTION || state == IAttackRegistry.ContractState.CORRUPTED
        ) {
            revert StakingClosed();
        }
    }
```

**File:** src/ConfidencePool.sol (L793-795)
```text
        if (riskWindowStart == 0 && _isActiveRiskState(state)) {
            _markRiskWindowStart();
        }
```

**File:** src/ConfidencePool.sol (L801-816)
```text
    function _markRiskWindowStart() internal {
        // Cap at expiry: accrual is bounded by the pool's lifecycle. Without the cap, a late
        // observation could pin riskWindowStart > expiry, and `_clampUserSums` would record every
        // pre-risk deposit as entering past T = expiry (EXPIRED path). The k=2 sums then encode
        // post-deadline "at-risk" time that never actually existed.
        uint256 t = block.timestamp;
        if (t > expiry) t = expiry;
        // Cast is truncation-safe: `t` is capped at `expiry`, which is itself a uint32.
        // forge-lint: disable-next-line(unsafe-typecast)
        riskWindowStart = uint32(t);
        // Eagerly reset the global accumulators so every currently-eligible staker is treated as
        // entering at `t`. Per-user sums stay stale until `_clampUserSums` runs on the next op
        // touching that user.
        sumStakeTime = totalEligibleStake * t;
        sumStakeTimeSq = totalEligibleStake * t * t;
        emit RiskWindowStarted(t);
```
