### Title
Unclaimed staker principal permanently locked after SURVIVED/EXPIRED resolution — (`File: src/ConfidencePool.sol`)

### Summary
After a pool resolves as SURVIVED or EXPIRED, any staker who never calls `claimSurvived()` or `claimExpired()` has their principal permanently locked in the contract. `sweepUnclaimedBonus()` explicitly reserves `totalEligibleStake` and cannot touch it, and no other function exists to recover it.

### Finding Description
`claimSurvived()` and `claimExpired()` are strictly `msg.sender`-gated: only the staker themselves can pull their own principal out. [1](#0-0) 

`sweepUnclaimedBonus()` — the only post-resolution sweep path for SURVIVED/EXPIRED — explicitly carves out `totalEligibleStake` as a protected reserve and will only sweep the balance *above* that floor: [2](#0-1) 

Once `claimsStarted` is latched true (on the first claim), the moderator's re-flag window closes, so `flagOutcome` cannot redirect the outcome to CORRUPTED (which would allow `claimCorrupted` to sweep everything): [3](#0-2) 

There is no owner/moderator/admin function, no timeout-based sweep, and no proxy-claim mechanism that can move unclaimed principal out of the contract. The pool has no analogue to the CORRUPTED path's `sweepUnclaimedCorrupted()` for the SURVIVED/EXPIRED paths.

### Impact Explanation
Any staker who loses wallet access, abandons their position, or simply never claims after resolution has their full principal permanently frozen in the pool. The `recoveryAddress` — the sponsor's designated destination for all residual funds — cannot receive these tokens. The funds are irrecoverable by any actor in the system.

### Likelihood Explanation
Moderate. Pools can run for months (minimum 30-day expiry lead, no maximum). Stakers who deposited early and stopped monitoring, or who lose key access over a long pool lifetime, will not claim. Even a single non-claiming staker permanently strands their principal. The protocol's permissionless design (no KYC, open staking) increases the probability of abandoned positions.

### Recommendation
Add a time-gated sweep for unclaimed principal after SURVIVED/EXPIRED resolution, analogous to `sweepUnclaimedCorrupted()` on the CORRUPTED path. After a sufficient grace window (e.g., `expiry + CORRUPTED_CLAIM_WINDOW`), allow anyone to call a `sweepUnclaimedPrincipal()` function that transfers the remaining `totalEligibleStake` balance to `recoveryAddress` and zeroes the accounting. This mirrors the fix applied to the FPAM issue: rather than leaving funds with no exit path, provide a defined, time-bounded recovery route.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

// Scenario: two stakers deposit; pool resolves SURVIVED; only one claims.
// The other staker's principal is permanently locked.

function test_UnclaimedPrincipalStuckAfterSurvived() public {
    // --- Setup: create pool, two stakers deposit ---
    // staker1 deposits 100e18, staker2 deposits 50e18
    vm.prank(staker1);
    pool.stake(100e18);

    vm.prank(staker2);
    pool.stake(50e18);

    // Bonus contributed
    vm.prank(bonusContributor);
    pool.contributeBonus(10e18);

    // Registry transitions to PRODUCTION (terminal)
    // Moderator flags SURVIVED
    vm.prank(moderator);
    pool.flagOutcome(PoolStates.Outcome.SURVIVED, false, address(0));

    // staker1 claims — succeeds
    vm.prank(staker1);
    pool.claimSurvived();

    // staker2 never claims (lost key / abandoned position)
    // totalEligibleStake is now 50e18 (staker2's unclaimed principal)

    // sweepUnclaimedBonus cannot touch it — reserved = totalEligibleStake = 50e18
    // freeBalance ≈ 50e18 + remaining bonus dust
    // amount = freeBalance - reserved ≈ 0 (or only dust above 50e18)
    vm.expectRevert(IConfidencePool.NothingToSweep.selector);
    pool.sweepUnclaimedBonus(); // reverts or sweeps 0 — staker2's 50e18 is stuck

    // No other function can move staker2's 50e18 out.
    // It sits in the contract forever.
    assertEq(stakeToken.balanceOf(address(pool)), 50e18 /* + dust */);
}
```

The `reserved` computation at line 484 sets `reserved = totalEligibleStake = 50e18`, so `amount = freeBalance − reserved = 0`, and `sweepUnclaimedBonus` reverts `NothingToSweep`. [4](#0-3)  The 50e18 of staker2's principal has no exit path.

### Citations

**File:** src/ConfidencePool.sol (L327-327)
```text
        if (outcome != PoolStates.Outcome.UNRESOLVED && claimsStarted) revert OutcomeAlreadySet();
```

**File:** src/ConfidencePool.sol (L386-403)
```text
        uint256 userEligible = eligibleStake[msg.sender];
        if (userEligible == 0) revert InvalidAmount();

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
```

**File:** src/ConfidencePool.sol (L482-493)
```text
        uint256 reserved;
        if (totalEligibleStake != 0) {
            reserved = totalEligibleStake;
            if (riskWindowStart != 0) {
                reserved += snapshotTotalBonus - claimedBonus;
            }
        }

        // aderyn-fp-next-line(reentrancy-state-change)
        uint256 freeBalance = stakeToken.balanceOf(address(this));
        uint256 amount = freeBalance > reserved ? freeBalance - reserved : 0;
        if (amount == 0) revert NothingToSweep();
```
