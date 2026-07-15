### Title
Wrong Event Emitted in `claimExpired` SURVIVED Auto-Resolution Branch — (`File: src/ConfidencePool.sol`)

### Summary
When `claimExpired` auto-resolves the pool to `SURVIVED` (registry in `PRODUCTION` state), it emits `ClaimSurvived` instead of `ClaimExpired`. The `EXPIRED` branch of the same function correctly emits `ClaimExpired`. This inconsistency means off-chain indexers listening to `ClaimExpired` to track all claims made through `claimExpired` will silently miss the SURVIVED auto-resolution case.

### Finding Description
`claimExpired` contains three auto-resolution branches. The `EXPIRED` branch at line 605 emits `ClaimExpired`. The `SURVIVED` branch at line 603 emits `ClaimSurvived`. Both branches execute the identical payout logic (principal + k=2 bonus share) and are reached exclusively through the `claimExpired` entry point. [1](#0-0) 

The `SURVIVED` branch is only reachable from `claimExpired` — once `outcome` is set to `SURVIVED` by the first caller, all subsequent stakers are gated out of `claimExpired` by the `InvalidOutcome` revert at line 514 and must use `claimSurvived` instead. [2](#0-1) 

So the first caller who triggers auto-resolution to `SURVIVED` has their claim recorded under `ClaimSurvived`, while every subsequent staker (using `claimSurvived`) also emits `ClaimSurvived`. Off-chain services cannot distinguish "auto-resolved SURVIVED via `claimExpired`" from "moderator-flagged SURVIVED via `claimSurvived`", and any indexer tracking `ClaimExpired` to audit all `claimExpired` activity will have a silent gap. [3](#0-2) 

### Impact Explanation
The impact is confined to off-chain observability. No funds are at risk and no on-chain invariant is broken. However, indexers, analytics dashboards, or keeper bots that filter `ClaimExpired` events to reconstruct the full set of `claimExpired` calls will produce an incomplete record whenever the pool auto-resolves to `SURVIVED`. This mirrors the exact class of harm described in the reference report: mixing two semantically distinct actions under one event type hinders filtering and auditing.

### Likelihood Explanation
The condition requires the registry to be in `PRODUCTION` state at the moment the first post-expiry `claimExpired` call is made (i.e., the moderator did not call `flagOutcome` before expiry, and the registry reached `PRODUCTION` before any staker called `claimExpired`). This is a plausible operational path — it is the normal "pool expired, agreement survived, no moderator action needed" scenario — so the likelihood is moderate.

### Recommendation
In the `SURVIVED` auto-resolution branch of `claimExpired`, emit `ClaimExpired` instead of `ClaimSurvived`. The payout semantics are identical; only the event label needs to change so that all claims originating from `claimExpired` are uniformly indexed under `ClaimExpired` regardless of the auto-resolved outcome.

```solidity
// current (line 602-606)
if (outcome == PoolStates.Outcome.SURVIVED) {
    emit ClaimSurvived(msg.sender, userEligible, bonusShare);
} else {
    emit ClaimExpired(msg.sender, userEligible, bonusShare);
}

// recommended
emit ClaimExpired(msg.sender, userEligible, bonusShare);
```

### Proof of Concept
1. Deploy a pool; staker Alice stakes.
2. Pool reaches `expiry` without the moderator calling `flagOutcome`.
3. Registry is in `PRODUCTION` state.
4. Alice calls `claimExpired()`.
   - `outcome` is `UNRESOLVED` → enters the resolution block.
   - Registry state is `PRODUCTION` → branch at line 557 fires: `outcome = SURVIVED`, emits `OutcomeFlagged(address(0), SURVIVED, false, address(0))`.
   - Falls through to claim logic; emits **`ClaimSurvived(Alice, principal, bonus)`** — not `ClaimExpired`.
5. An off-chain indexer filtering `ClaimExpired` events sees zero events for this pool, even though `claimExpired` was called and funds moved.
6. A second staker Bob calls `claimExpired()` → reverts `InvalidOutcome` (outcome is now `SURVIVED`). Bob must call `claimSurvived()`, which also emits `ClaimSurvived`. The indexer still sees no `ClaimExpired`. [4](#0-3)

### Citations

**File:** src/ConfidencePool.sol (L513-515)
```text
        if (block.timestamp < expiry) revert PoolNotExpired();
        if (outcome != PoolStates.Outcome.UNRESOLVED && outcome != PoolStates.Outcome.EXPIRED) {
            revert InvalidOutcome();
```

**File:** src/ConfidencePool.sol (L557-606)
```text
            if (state == IAttackRegistry.ContractState.PRODUCTION) {
                outcome = PoolStates.Outcome.SURVIVED;
                outcomeFlaggedAt = riskWindowEnd;
                emit OutcomeFlagged(address(0), PoolStates.Outcome.SURVIVED, false, address(0));
            } else {
                // Reached for EVERY non-terminal state, including active-risk (UNDER_ATTACK /
                // PROMOTION_REQUESTED). Intentional, NOT a missing active-risk deferral: expiring
                // while still attackable means the agreement survived the term, so EXPIRED
                // (principal + bonus returned) is correct. See docs/DESIGN.md (EXPIRED resolution).
                outcome = PoolStates.Outcome.EXPIRED;
                // EXPIRED has no terminal registry observation; `expiry` is the pool's own
                // deadline and is fixed at init/lock time, so it's grief-proof as the upper bound.
                outcomeFlaggedAt = expiry;
                emit OutcomeFlagged(address(0), PoolStates.Outcome.EXPIRED, false, address(0));
            }
            // Defense-in-depth: mirror the auto-CORRUPTED lock above so finality of mechanical
            // resolution is uniform across all three branches and doesn't depend on the
            // registry's one-way state machine to block a later moderator override.
            claimsStarted = true;
        }

        if (hasClaimed[msg.sender]) revert InvalidAmount();

        uint256 userEligible = eligibleStake[msg.sender];
        if (userEligible == 0) {
            // Soft-success: caller had nothing to claim, but the outcome is now terminal —
            // useful for a non-staker to mechanically auto-resolve the pool post-expiry.
            return;
        }

        _clampUserSums(msg.sender);

        hasClaimed[msg.sender] = true;

        uint256 bonusShare = _bonusShare(msg.sender, userEligible);
        uint256 payout = userEligible + bonusShare;
        totalEligibleStake -= userEligible;
        claimedBonus += bonusShare;

        delete eligibleStake[msg.sender];
        delete userSumStakeTime[msg.sender];
        delete userSumStakeTimeSq[msg.sender];

        if (!claimsStarted) claimsStarted = true;
        stakeToken.safeTransfer(msg.sender, payout);
        if (outcome == PoolStates.Outcome.SURVIVED) {
            emit ClaimSurvived(msg.sender, userEligible, bonusShare);
        } else {
            emit ClaimExpired(msg.sender, userEligible, bonusShare);
        }
```

**File:** src/interfaces/IConfidencePool.sol (L13-20)
```text
    event ClaimSurvived(address indexed staker, uint256 principal, uint256 bonusShare);
    event ClaimCorrupted(address indexed caller, address indexed recoveryAddress, uint256 amount);
    event AttackerBountyClaimed(
        address indexed attacker, uint256 amount, uint256 totalClaimed, uint256 totalEntitlement
    );
    event UnclaimedCorruptedSwept(address indexed caller, address indexed recoveryAddress, uint256 amount);
    event BonusSwept(address indexed caller, address indexed recoveryAddress, uint256 amount);
    event ClaimExpired(address indexed staker, uint256 principal, uint256 bonusShare);
```
