### Title
Sponsor redirects bad-faith CORRUPTED sweep by changing `recoveryAddress` after `flagOutcome` — (File: src/ConfidencePool.sol)

### Summary
`setRecoveryAddress` carries no post-resolution guard, so the pool owner (sponsor) can replace `recoveryAddress` after the moderator calls `flagOutcome(CORRUPTED, false, …)` and before `claimCorrupted` (or `sweepUnclaimedCorrupted`) executes. Because those sweep functions read `recoveryAddress` live rather than from a snapshot, the sponsor can silently redirect the entire pool — stakers' principal plus bonus — to an address of their choosing, defeating the bad-faith CORRUPTED punishment mechanism.

### Finding Description
`flagOutcome` snapshots the accounting state needed for bonus distribution (`snapshotTotalStaked`, `snapshotTotalBonus`, `snapshotSumStakeTime`, `snapshotSumStakeTimeSq`) but does **not** snapshot `recoveryAddress`. Every sweep function then reads the live storage slot:

```solidity
// claimCorrupted  (line 423)
stakeToken.safeTransfer(recoveryAddress, toSweep);

// sweepUnclaimedCorrupted  (line 468)
stakeToken.safeTransfer(recoveryAddress, amount);

// sweepUnclaimedBonus  (line 506)
stakeToken.safeTransfer(recoveryAddress, amount);
```

`setRecoveryAddress` imposes no outcome-state guard:

```solidity
// line 611-618
function setRecoveryAddress(address newRecoveryAddress) external onlyOwner {
    if (newRecoveryAddress == address(0)) revert InvalidRecoveryAddress();
    address oldRecoveryAddress = recoveryAddress;
    recoveryAddress = newRecoveryAddress;
    emit RecoveryAddressUpdated(oldRecoveryAddress, newRecoveryAddress);
}
```

A malicious sponsor therefore executes the following sequence:

1. At pool creation, set `recoveryAddress` to a credible address (e.g. a DAO multisig) so stakers trust the pool.
2. Allow the agreement to be breached; the moderator calls `flagOutcome(CORRUPTED, false, address(0))`.
3. Before any caller invokes `claimCorrupted`, call `setRecoveryAddress(sponsorControlledAddress)`.
4. Call (or let anyone call) `claimCorrupted` — the full balance transfers to the sponsor-controlled address.

Because `claimCorrupted` is permissionless and `claimsStarted` is set only inside it, the window between `flagOutcome` and the first `claimCorrupted` call is the attack surface. The sponsor can also apply the same technique to `sweepUnclaimedCorrupted` (after the 180-day bounty window) and `sweepUnclaimedBonus` (excess bonus under SURVIVED/EXPIRED).

### Impact Explanation
In the bad-faith CORRUPTED path the entire pool — stakers' principal plus the bonus pool — is supposed to flow to `recoveryAddress` as the protocol's punitive sweep. By redirecting it, the sponsor recovers all deposited capital (including stakers' principal) that the protocol intended to confiscate. The punishment mechanism is completely nullified: the sponsor suffers no economic consequence for the breach and profits from the stakers' deposits. The same defect lets the sponsor capture unclaimed bounty funds in the good-faith CORRUPTED path and excess bonus in SURVIVED/EXPIRED paths.

### Likelihood Explanation
Medium. The exploit requires a sponsor who deliberately presents a credible `recoveryAddress` at pool creation to attract stakers, then switches it after the moderator flags the outcome. The on-chain steps are trivial (one `setRecoveryAddress` call), require no special tooling, and can be executed in the same block as or immediately after `flagOutcome`. The only friction is that the moderator must flag before the sponsor acts, but the sponsor monitors the mempool and can front-run `claimCorrupted` with high confidence on any chain with a public mempool.

### Recommendation
Lock `recoveryAddress` once the outcome is no longer `UNRESOLVED`. The minimal fix is a guard at the top of `setRecoveryAddress`:

```solidity
function setRecoveryAddress(address newRecoveryAddress) external onlyOwner {
    if (outcome != PoolStates.Outcome.UNRESOLVED) revert OutcomeAlreadySet();
    if (newRecoveryAddress == address(0)) revert InvalidRecoveryAddress();
    …
}
```

Alternatively, snapshot `recoveryAddress` inside `flagOutcome` (alongside the other accounting fields) and use the snapshotted value in all three sweep functions, mirroring how `snapshotTotalStaked` is used instead of the live `totalEligibleStake`.

### Proof of Concept

```
1. Sponsor deploys pool: recoveryAddress = daoTreasury
2. Stakers deposit; pool accumulates totalEligibleStake = S, totalBonus = B
3. Agreement is breached; moderator calls:
       flagOutcome(CORRUPTED, goodFaith=false, attacker=address(0))
   → outcome = CORRUPTED, snapshotTotalStaked = S, snapshotTotalBonus = B
   → recoveryAddress is NOT snapshotted
4. Sponsor calls:
       setRecoveryAddress(sponsorWallet)   // no revert — no outcome guard
5. Anyone calls claimCorrupted():
       toSweep = stakeToken.balanceOf(address(this))  // = S + B
       stakeToken.safeTransfer(recoveryAddress, toSweep)
       // recoveryAddress == sponsorWallet  ← redirected
6. Sponsor receives S + B; daoTreasury receives nothing.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/ConfidencePool.sol (L354-378)
```text
        outcome = newOutcome;
        goodFaith = goodFaith_;
        attacker = attacker_;
        snapshotTotalStaked = totalEligibleStake;
        snapshotTotalBonus = totalBonus;
        snapshotSumStakeTime = sumStakeTime;
        snapshotSumStakeTimeSq = sumStakeTimeSq;
        corruptedReserve = newOutcome == PoolStates.Outcome.CORRUPTED ? snapshotTotalStaked + snapshotTotalBonus : 0;
        bountyEntitlement = willBeGoodFaithCorrupted ? snapshotTotalStaked + snapshotTotalBonus : 0;
        if (willBeGoodFaithCorrupted) {
            if (_firstGoodFaithCorruptedAt == 0) {
                // forge-lint: disable-next-line(unsafe-typecast)
                _firstGoodFaithCorruptedAt = uint32(block.timestamp);
            }
            // Reuses the original window on re-entry — which may already be in the past, leaving
            // nothing to claim. Intended: the deadline must never be extendable.
            // Sum stays in uint32 unless flagged within 180 days of the 2106 ceiling; out of scope.
            // forge-lint: disable-next-line(unsafe-typecast)
            corruptedClaimDeadline = uint32(_firstGoodFaithCorruptedAt + CORRUPTED_CLAIM_WINDOW);
        } else {
            corruptedClaimDeadline = 0;
        }
        outcomeFlaggedAt = riskWindowEnd;

        emit OutcomeFlagged(msg.sender, newOutcome, goodFaith_, attacker_);
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

**File:** src/ConfidencePool.sol (L456-470)
```text
    function sweepUnclaimedCorrupted() external nonReentrant {
        if (outcome != PoolStates.Outcome.CORRUPTED) revert OutcomeNotSet();
        if (!goodFaith) revert NotGoodFaithCorrupted();
        if (block.timestamp <= corruptedClaimDeadline) revert ClaimWindowNotExpired();

        // aderyn-fp-next-line(reentrancy-state-change)
        uint256 amount = stakeToken.balanceOf(address(this));
        if (amount == 0) revert NothingToSweep();

        corruptedReserve = 0;
        bountyClaimed = bountyEntitlement;
        if (!claimsStarted) claimsStarted = true;
        stakeToken.safeTransfer(recoveryAddress, amount);

        emit UnclaimedCorruptedSwept(msg.sender, recoveryAddress, amount);
```

**File:** src/ConfidencePool.sol (L490-507)
```text
        // aderyn-fp-next-line(reentrancy-state-change)
        uint256 freeBalance = stakeToken.balanceOf(address(this));
        uint256 amount = freeBalance > reserved ? freeBalance - reserved : 0;
        if (amount == 0) revert NothingToSweep();

        // Bonus is only unreserved when no staker is owed it (no risk window, or no stakers left).
        // In that case the sweep removes it from the pool, so drop it from the live `totalBonus`
        // too — keeping the accounting honest for any later re-snapshot. Clamp to `totalBonus` so
        // swept donations/dust (never counted in it) can't over-decrement or underflow.
        if (totalEligibleStake == 0 || riskWindowStart == 0) {
            totalBonus -= amount <= totalBonus ? amount : totalBonus;
        }

        // Intentionally does NOT set claimsStarted. A direct-transfer donation of as little as 1
        // wei would otherwise let anyone flip the flag post-flagOutcome and block the moderator's
        // documented pre-claim re-flag window. Genuine reliance only comes from claim entrypoints.
        stakeToken.safeTransfer(recoveryAddress, amount);

```

**File:** src/ConfidencePool.sol (L611-618)
```text
    function setRecoveryAddress(address newRecoveryAddress) external onlyOwner {
        if (newRecoveryAddress == address(0)) revert InvalidRecoveryAddress();

        address oldRecoveryAddress = recoveryAddress;
        recoveryAddress = newRecoveryAddress;

        emit RecoveryAddressUpdated(oldRecoveryAddress, newRecoveryAddress);
    }
```
