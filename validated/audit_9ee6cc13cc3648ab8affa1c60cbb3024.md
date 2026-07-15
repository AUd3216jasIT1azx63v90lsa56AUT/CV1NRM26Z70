### Title
Absent deadline on `withdraw()` lets a malicious validator permanently lock stakers out of their exit — (File: src/ConfidencePool.sol)

### Summary
`withdraw()` has no caller-supplied deadline. A malicious block producer can hold a staker's withdrawal transaction in the mempool until the registry transitions from `ATTACK_REQUESTED` to `UNDER_ATTACK`, at which point the call reverts with `WithdrawsDisabled` and the staker is permanently locked in the pool with no exit path.

### Finding Description
`withdraw()` gates on two conditions: `riskWindowStart == 0` and the live registry state being one of `NOT_DEPLOYED`, `NEW_DEPLOYMENT`, or `ATTACK_REQUESTED`. [1](#0-0) 

Neither condition is time-bounded by the caller. Because there is no `deadline` argument, the transaction is valid at any future block timestamp. A malicious validator (or a centralized L2 sequencer) can observe a pending `withdraw()` in the mempool, withhold it, wait for the registry to advance to `UNDER_ATTACK`, and then include the transaction. At that point `_observePoolState()` sets `riskWindowStart = block.timestamp` [2](#0-1) [3](#0-2) 

and the `riskWindowStart != 0` branch immediately reverts the withdrawal. The one-way latch then permanently prevents any future `withdraw()` call by the same staker. [4](#0-3) 

The same class of manipulation applies to `stake()` and `contributeBonus()`: a validator can hold those transactions until `_assertDepositsAllowed` would revert them (e.g., after `PROMOTION_REQUESTED` or `CORRUPTED`), causing the deposit to silently fail. The impact there is a failed transaction rather than a fund lock, so `withdraw()` is the critical path.

### Impact Explanation
A staker who legitimately intended to exit during the pre-attack window is permanently locked into the pool. If the pool subsequently resolves as bad-faith CORRUPTED, the staker loses 100% of their principal and any bonus to `recoveryAddress`. [5](#0-4) 

Even under a SURVIVED or EXPIRED resolution the staker suffers the loss of their intended exit: they are forced to bear the full risk window they explicitly tried to avoid, with no recourse.

### Likelihood Explanation
BattleChain is described as an EVM-compatible L2. [6](#0-5) 

L2s commonly operate with a single centralized sequencer that has unilateral control over transaction ordering and inclusion timing. The transition from `ATTACK_REQUESTED` to `UNDER_ATTACK` is a discrete, observable on-chain event; a sequencer can trivially time the inclusion of a held transaction to land in the block immediately after that transition. No mempool competition or probabilistic timing is required — the sequencer simply reorders.

### Recommendation
Add a `deadline` parameter to `withdraw()` (and, for completeness, `stake()` and `contributeBonus()`) and revert if `block.timestamp > deadline`:

```solidity
function withdraw(uint256 deadline) external nonReentrant {
    if (block.timestamp > deadline) revert DeadlineExpired();
    // ... existing logic
}
```

This mirrors the fix applied in the referenced Uniswap v3 limit-order audit: callers express the latest block timestamp at which their transaction is still meaningful, and any validator-induced delay past that point causes a clean revert rather than a state-changing execution under adversarial conditions.

### Proof of Concept

1. Registry is in `ATTACK_REQUESTED`. Staker calls `withdraw()` and the transaction enters the mempool.
2. Malicious sequencer withholds the transaction.
3. Registry advances to `UNDER_ATTACK` (e.g., the BattleChain DAO calls `markUnderAttack()`).
4. Sequencer includes the staker's `withdraw()` in the next block.
5. Inside `withdraw()`, `_observePoolState()` reads `UNDER_ATTACK` and calls `_markRiskWindowStart()`, setting `riskWindowStart = block.timestamp`. [7](#0-6) 
6. The guard `riskWindowStart != 0` is now true → `revert WithdrawsDisabled()`. [4](#0-3) 
7. The staker's `eligibleStake` remains non-zero; all future `withdraw()` calls revert identically because `riskWindowStart` is a one-way latch.
8. Moderator later calls `flagOutcome(CORRUPTED, false, address(0))`. `claimCorrupted()` sweeps the entire pool balance — including the staker's principal — to `recoveryAddress`. [5](#0-4)

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

**File:** src/ConfidencePool.sol (L408-426)
```text
    function claimCorrupted() external nonReentrant {
        if (outcome != PoolStates.Outcome.CORRUPTED) revert OutcomeNotSet();
        if (goodFaith && bountyClaimed < bountyEntitlement) revert MustClaimBountyFirst();

        // aderyn-fp-next-line(reentrancy-state-change)
        uint256 toSweep = stakeToken.balanceOf(address(this));
        if (toSweep == 0) revert NothingToSweep();

        // Clamp the decrement — `toSweep` can exceed the original reserve when post-resolution
        // donations have inflated the balance.
        corruptedReserve = toSweep <= corruptedReserve ? corruptedReserve - toSweep : 0;
        if (!goodFaith) {
            bountyClaimed = bountyEntitlement;
        }
        if (!claimsStarted) claimsStarted = true;
        stakeToken.safeTransfer(recoveryAddress, toSweep);

        emit ClaimCorrupted(msg.sender, recoveryAddress, toSweep);
    }
```

**File:** src/ConfidencePool.sol (L793-798)
```text
        if (riskWindowStart == 0 && _isActiveRiskState(state)) {
            _markRiskWindowStart();
        }
        if (riskWindowEnd == 0 && _isTerminalState(state)) {
            _markRiskWindowEnd();
        }
```

**File:** src/ConfidencePool.sol (L806-810)
```text
        uint256 t = block.timestamp;
        if (t > expiry) t = expiry;
        // Cast is truncation-safe: `t` is capped at `expiry`, which is itself a uint32.
        // forge-lint: disable-next-line(unsafe-typecast)
        riskWindowStart = uint32(t);
```

**File:** README.md (L126-130)
```markdown
  Blockchains:
      - BattleChain (EVM-compatible L2)
  Solidity:
      - 0.8.26 (via-IR, optimizer 200 runs)
  Tokens:
```
