### Title
Sponsor can redirect CORRUPTED sweep by updating `recoveryAddress` after `flagOutcome`, stealing stakers' principal — (File: src/ConfidencePool.sol)

### Summary
`setRecoveryAddress` has no guard preventing changes after the outcome is set. A malicious sponsor can change `recoveryAddress` between `flagOutcome(CORRUPTED, ...)` and the permissionless `claimCorrupted` / `sweepUnclaimedCorrupted` calls, redirecting the entire pool balance — including all stakers' principal — to an attacker-controlled address.

### Finding Description
`setRecoveryAddress` enforces only a zero-address check:

```solidity
function setRecoveryAddress(address newRecoveryAddress) external onlyOwner {
    if (newRecoveryAddress == address(0)) revert InvalidRecoveryAddress();
    recoveryAddress = newRecoveryAddress;
    ...
}
``` [1](#0-0) 

There is no guard on `outcome`, `claimsStarted`, or any other lifecycle latch. By contrast, `expiry` is explicitly protected by the one-way `expiryLocked` flag once the first stake arrives (DESIGN.md §10), but `recoveryAddress` carries no equivalent protection. [2](#0-1) 

`claimCorrupted` and `sweepUnclaimedCorrupted` both read `recoveryAddress` live at call time — it is never snapshotted alongside `snapshotTotalStaked` / `snapshotTotalBonus` at `flagOutcome`:

```solidity
stakeToken.safeTransfer(recoveryAddress, toSweep);   // claimCorrupted
...
stakeToken.safeTransfer(recoveryAddress, amount);    // sweepUnclaimedCorrupted
``` [3](#0-2) [4](#0-3) 

Attack path (bad-faith CORRUPTED):

1. Sponsor deploys a pool with `recoveryAddress = legitimateProtocolAddress` to attract stakers.
2. Stakers deposit, verifying on-chain parameters including `recoveryAddress`.
3. Agreement is corrupted; moderator calls `flagOutcome(CORRUPTED, false, address(0))`.
4. Sponsor immediately calls `setRecoveryAddress(sponsorOwnedAddress)` — no revert, no guard.
5. Sponsor (or anyone) calls `claimCorrupted()` → entire pool balance transferred to `sponsorOwnedAddress`.
6. All stakers lose 100% of principal.

The sponsor can execute steps 4–5 atomically in a single transaction or front-run any competing `claimCorrupted` call, making the race trivially winnable.

The same defect applies to `sweepUnclaimedBonus` (excess bonus under SURVIVED/EXPIRED) and `sweepUnclaimedCorrupted` (remainder after attacker bounty under good-faith CORRUPTED). [5](#0-4) 

### Impact Explanation
Under bad-faith CORRUPTED resolution, `claimCorrupted` sweeps `stakeToken.balanceOf(address(this))` — the full pool — to `recoveryAddress`. A sponsor who changes `recoveryAddress` post-`flagOutcome` redirects every staker's principal to themselves. Impact is total, irreversible loss of principal for all stakers in the pool.

### Likelihood Explanation
The sponsor is the pool owner and has unconditional write access to `recoveryAddress` via `setRecoveryAddress`. The attack requires only that the moderator flag CORRUPTED (bad-faith), after which the sponsor can act atomically. The sponsor is not a trusted actor under the protocol's adversarial model (only the moderator/DAO and factory owner are designated trusted); the protocol explicitly expects stakers to verify pool parameters before depositing, but that verification is rendered meaningless if `recoveryAddress` can be silently swapped post-deposit.

### Recommendation
Snapshot `recoveryAddress` at `flagOutcome` time into a new storage variable (e.g., `snapshotRecoveryAddress`), mirroring the existing snapshot pattern for `snapshotTotalStaked` and `snapshotTotalBonus`. Use the snapshot — not the live field — in `claimCorrupted`, `sweepUnclaimedCorrupted`, and `sweepUnclaimedBonus`.

Alternatively, add a one-way latch (analogous to `expiryLocked`) that permanently blocks `setRecoveryAddress` once `outcome != UNRESOLVED` or once `claimsStarted` is true.

### Proof of Concept

```solidity
// 1. Deploy pool with recoveryAddress = legitimateAddress
// 2. Stakers deposit
// 3. Moderator flags bad-faith CORRUPTED
pool.flagOutcome(PoolStates.Outcome.CORRUPTED, false, address(0));

// 4. Sponsor redirects sweep — no revert
pool.setRecoveryAddress(sponsorAddress);

// 5. Anyone sweeps — funds go to sponsorAddress, not legitimateAddress
pool.claimCorrupted();

assert(stakeToken.balanceOf(sponsorAddress) == entirePoolBalance);
assert(stakeToken.balanceOf(legitimateAddress) == 0);
``` [6](#0-5) [7](#0-6)

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

**File:** src/ConfidencePool.sol (L468-468)
```text
        stakeToken.safeTransfer(recoveryAddress, amount);
```

**File:** src/ConfidencePool.sol (L506-506)
```text
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

**File:** docs/DESIGN.md (L255-261)
```markdown
- **`recoveryAddress`** — CORRUPTED sweep destination. Receives the full pool (including stakers'
  principal) under bad-faith CORRUPTED; only excess/dust under SURVIVED/EXPIRED/good-faith.
- **`expiry`** — sponsor-mutable only **until the first stake** (one-way `expiryLocked` latch).
  This protects staker reliance: once anyone has deposited against a given deadline (which feeds
  the k=2 weighting as `T` for the EXPIRED path), the sponsor cannot move it. The latch
  intentionally does **not** reset when stake is withdrawn — resetting it would let the sponsor
  move `expiry` during an all-stakers-exited moment and harm the next cohort.
```
