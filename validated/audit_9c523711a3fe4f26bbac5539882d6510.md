### Title
`riskWindowStart` latch silently rolls back inside `withdraw()`, allowing principal escape after a registry rewind — (File: src/ConfidencePool.sol)

### Summary
`withdraw()` calls `_observePoolState()`, which writes `riskWindowStart` when the registry is `UNDER_ATTACK`, then immediately reverts with `WithdrawsDisabled`. Because the revert unwinds the entire transaction, `riskWindowStart` is never persisted. If the registry subsequently rewinds to a pre-attack state and no other call has latched `riskWindowStart` in the interim, a staker can call `withdraw()` again and succeed — bypassing the one-way lock the design explicitly relies on.

### Finding Description
`withdraw()` is structured as:

```
_observePoolState()   // may write riskWindowStart
check condition       // riskWindowStart != 0 → revert WithdrawsDisabled
``` [1](#0-0) 

When the registry is `UNDER_ATTACK`, `_observePoolState()` calls `_markRiskWindowStart()`, which writes `riskWindowStart` and resets the global accumulators. [2](#0-1) [3](#0-2) 

The condition at line 294 then evaluates `riskWindowStart != 0` as `true` and reverts. EVM semantics roll back every storage write in the transaction, so `riskWindowStart` returns to `0`. The latch was never durably set.

`pokeRiskWindow()` is the intended non-reverting path to seal the latch: [4](#0-3) 

But `pokeRiskWindow()` is permissionless and not guaranteed to be called. If the only pool interactions during the `UNDER_ATTACK` window are `withdraw()` attempts (all of which revert and roll back), `riskWindowStart` remains `0` throughout.

DESIGN.md §9 and §11 both state: *"A benign upstream state rewind cannot re-open withdraw: that is gated on the one-way `riskWindowStart != 0` latch, not solely on live state."* The registry is explicitly acknowledged to be capable of rewinding. If it rewinds to `NEW_DEPLOYMENT` or `ATTACK_REQUESTED` while `riskWindowStart == 0`, the second condition in `withdraw()`'s guard is also false, and the withdrawal succeeds.

### Impact Explanation
A staker can recover their full principal after the risk window has opened. In the CORRUPTED resolution path, the moderator flags the pool expecting `snapshotTotalStaked + snapshotTotalBonus` to be present; if stakers have already withdrawn, the pool is undercollateralized and the recovery address or named attacker receives less than the full entitlement. The design guarantee that "withdrawals are permanently disabled from `UNDER_ATTACK` onward" is violated.

### Likelihood Explanation
Three conditions must coincide: (1) the registry transitions to `UNDER_ATTACK`; (2) no call to `stake()` or `pokeRiskWindow()` durably latches `riskWindowStart` during that window — only `withdraw()` calls occur, all of which revert; (3) the registry rewinds to a pre-attack state. DESIGN.md §11 explicitly acknowledges registry rewinds as a real possibility the latch is meant to guard against. In a pool with a single staker who monitors the registry and acts adversarially, conditions (1)–(3) are achievable without any privileged access.

### Recommendation
Separate the state-sealing step from the reverting guard. The simplest fix is to read the live registry state with a plain `_getAgreementState()` call (no side effects) for the withdrawal gate, and rely on `pokeRiskWindow()` / `stake()` to seal `riskWindowStart` in their own non-reverting transactions. Alternatively, check the live state before calling `_observePoolState()` in `withdraw()`: if the state is already `UNDER_ATTACK` or later, revert immediately without invoking `_observePoolState()`, so the latch write is never attempted in a doomed transaction. Either approach eliminates the silent rollback and makes the latch's persistence independent of the reverting path.

### Proof of Concept
```
1. Alice stakes 100 tokens. Pool is in NEW_DEPLOYMENT; riskWindowStart == 0.
2. Registry transitions to UNDER_ATTACK.
3. Alice calls withdraw().
   - _observePoolState() writes riskWindowStart = block.timestamp.
   - Guard: riskWindowStart != 0 → revert WithdrawsDisabled.
   - EVM rolls back: riskWindowStart = 0 again.
4. No other actor calls pokeRiskWindow() or stake() during the UNDER_ATTACK window.
5. Registry rewinds to NEW_DEPLOYMENT (or ATTACK_REQUESTED).
6. Alice calls withdraw() again.
   - _observePoolState(): state == NEW_DEPLOYMENT → riskWindowStart unchanged (0).
   - Guard: riskWindowStart == 0 AND state is pre-attack → condition is false.
   - Withdrawal proceeds; Alice receives 100 tokens.
7. Registry later transitions to CORRUPTED.
8. Moderator calls flagOutcome(CORRUPTED, ...).
   - snapshotTotalStaked == 0 (Alice already withdrew).
   - Recovery address or attacker receives 0 tokens.
```

### Citations

**File:** src/ConfidencePool.sol (L288-300)
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
```

**File:** src/ConfidencePool.sol (L649-658)
```text
    function pokeRiskWindow() external {
        // No-op once resolved: the snapshot globals are frozen, so the risk-window markers must
        // be too.
        if (outcome != PoolStates.Outcome.UNRESOLVED) return;
        // Revert only when nothing has been sealed — registry never reached active risk or
        // a terminal state.
        // aderyn-ignore-next-line(unchecked-return)
        _observePoolState();
        if (riskWindowStart == 0 && riskWindowEnd == 0) revert RiskWindowNotReached();
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
