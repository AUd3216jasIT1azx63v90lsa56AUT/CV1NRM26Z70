### Title
`_clampUserSums` fails to clamp pre-risk deposits when the user also holds post-risk deposits, inflating `userScore` above `globalScore` and allowing a staker to drain more than the entire bonus pool — (`File: src/ConfidencePool.sol`)

### Summary

`_clampUserSums` uses `eligibleStake[u]` (total stake, pre-risk + post-risk) as the multiplier in its clamp condition. When a user has both a pre-risk deposit `A` (at time `t_A < riskWindowStart`) and a post-risk deposit `B` (at time `t_B ≥ riskWindowStart`), the condition `userSumStakeTime[u] < eligibleStake[u] * riskWindowStart` can evaluate to `false` even though the pre-risk portion still needs to be clamped. The per-user sums are then left unclamped, while the global snapshot (`snapshotSumStakeTime`, `snapshotSumStakeTimeSq`) was correctly reset by `_markRiskWindowStart` to reflect clamped values. This creates a numerator/denominator mismatch in `_bonusShare`: the user's `userScore` is inflated above the correct value, while `globalScore` equals the sum of correct user scores. In the single-staker case, `userScore > globalScore`, so `bonusShare > snapshotTotalBonus` — the user is entitled to more than the entire bonus pool.

### Finding Description

`_markRiskWindowStart` eagerly resets the global accumulators:

```solidity
sumStakeTime = totalEligibleStake * t;
sumStakeTimeSq = totalEligibleStake * t * t;
``` [1](#0-0) 

This correctly treats every pre-risk staker as entering at `riskWindowStart`. Post-risk stakes then add their actual entry times to `sumStakeTime` and `sumStakeTimeSq` via `stake()`:

```solidity
sumStakeTime += contribTime;   // received * newEntry, newEntry >= riskWindowStart
sumStakeTimeSq += contribTimeSq;
``` [2](#0-1) 

So `snapshotSumStakeTime` = `A * riskWindowStart + B * t_B` (correct clamped value for a user with pre-risk `A` and post-risk `B`).

`_clampUserSums` is supposed to lazily apply the same clamp to per-user sums at claim time:

```solidity
if (userSumStakeTime[u] < stake_ * start) {
    userSumStakeTime[u] = stake_ * start;
    userSumStakeTimeSq[u] = stake_ * start * start;
}
``` [3](#0-2) 

The condition uses `stake_ = eligibleStake[u] = A + B`. For the user above, `userSumStakeTime[u] = A * t_A + B * t_B`. The condition becomes:

```
A * t_A + B * t_B  <  (A + B) * riskWindowStart
```

When `B * (t_B − riskWindowStart) > A * (riskWindowStart − t_A)` — i.e., the post-risk deposit is large relative to the pre-risk one — the condition is **false** and no clamping occurs. `userSumStakeTime[u]` stays at `A * t_A + B * t_B` instead of the correct `A * riskWindowStart + B * t_B`.

In `_bonusShare`, the user score is:

```
userScore = T² * userEligible − 2T * userSumStakeTime[u] + userSumStakeTimeSq[u]
``` [4](#0-3) 

Because `userSumStakeTime[u]` is too small (pre-risk deposit not clamped up to `riskWindowStart`), `userMinus` is too small, and `userScore` is inflated. The net inflation is:

```
actual_userScore − correct_userScore = A * (riskWindowStart − t_A) * (2T − riskWindowStart − t_A)
```

Both factors are strictly positive (since `T ≥ riskWindowStart > t_A`), so `actual_userScore > correct_userScore` always.

The denominator `globalScore` uses the snapshot values, which are correct:

```
globalScore = T² * snapshotTotalStaked − 2T * snapshotSumStakeTime + snapshotSumStakeTimeSq
``` [5](#0-4) 

`globalScore` equals the sum of all correct user scores. Because `actual_userScore > correct_userScore`, the ratio `actual_userScore / globalScore > correct_userScore / globalScore`, and the user receives more bonus than their fair share. In the single-staker case, `globalScore = correct_userScore < actual_userScore`, so `bonusShare = actual_userScore * snapshotTotalBonus / globalScore > snapshotTotalBonus` — the user is owed more than the entire bonus pool.

### Impact Explanation

A staker who holds both a pre-risk deposit and a post-risk deposit (staked during `UNDER_ATTACK`, which is explicitly permitted) receives an inflated `userScore`. In the single-staker scenario the computed `bonusShare` exceeds `snapshotTotalBonus`, causing the `safeTransfer` in `claimSurvived` or `claimExpired` to revert for insufficient balance (the user cannot claim at all). In the multi-staker scenario the inflated user drains a disproportionate share of the bonus pool, leaving later claimants unable to receive their full entitlement — the last claimants' transfers revert when the balance is exhausted. In both cases the bonus pool accounting is permanently broken after resolution.

### Likelihood Explanation

The trigger requires only two actions by the same address: one stake before `riskWindowStart` and one stake during `UNDER_ATTACK` (explicitly allowed by design). No privileged role is needed. The condition `B * (t_B − riskWindowStart) > A * (riskWindowStart − t_A)` is easily satisfied with a modestly larger second deposit. Any rational staker who deposited early and then adds capital after the risk window opens will unknowingly trigger this.

### Recommendation

`_clampUserSums` must clamp only the pre-risk portion of the user's stake. The correct approach is to track the user's pre-risk stake separately (e.g., a `userPreRiskStake` mapping set at the moment `_markRiskWindowStart` fires, or captured lazily). At clamp time, only that pre-risk amount is promoted to `riskWindowStart`; post-risk contributions already carry the correct entry time and must not be overwritten.

Alternatively, record the user's `userSumStakeTime` and `userSumStakeTimeSq` at the moment `_markRiskWindowStart` fires (eagerly, mirroring the global reset) so that the lazy clamp at claim time has a reliable pre-computed baseline to compare against, rather than the ambiguous `eligibleStake[u] * riskWindowStart` threshold that conflates pre-risk and post-risk stake.

### Proof of Concept

**Setup (single staker, concrete numbers):**

| Variable | Value |
|---|---|
| `riskWindowStart` | 1 000 |
| `riskWindowEnd` / `T` | 2 000 |
| Pre-risk deposit `A` | 100 tokens at `t_A = 500` |
| Post-risk deposit `B` | 1 000 tokens at `t_B = 1 500` |
| `snapshotTotalBonus` | 350 000 000 (arbitrary) |

**Global accumulators after `_markRiskWindowStart` + second stake:**

```
sumStakeTime    = 100 * 1000 + 1000 * 1500 = 1 600 000   (correct)
sumStakeTimeSq  = 100 * 1000² + 1000 * 1500² = 2 350 000 000   (correct)
snapshotTotalStaked = 1 100
```

**`_clampUserSums` at claim time:**

```
stake_ * start = 1100 * 1000 = 1 100 000
userSumStakeTime[u] = 100*500 + 1000*1500 = 1 550 000  ≥  1 100 000  → NO CLAMP
```

**`userScore` (actual, unclamped):**

```
userPlus  = 2000² * 1100 + (100*500² + 1000*1500²)
          = 4 400 000 000 + 2 275 000 000 = 6 675 000 000
userMinus = 2 * 2000 * 1 550 000 = 6 200 000 000
userScore = 6 675 000 000 − 6 200 000 000 = 475 000 000
```

**`globalScore` (correct):**

```
plus  = 2000² * 1100 + 2 350 000 000 = 6 750 000 000
minus = 2 * 2000 * 1 600 000 = 6 400 000 000
globalScore = 6 750 000 000 − 6 400 000 000 = 350 000 000
```

**`bonusShare`:**

```
bonusShare = mulDiv(475 000 000, 350 000 000, 350 000 000) = 475 000 000
```

The computed bonus (475 000 000) exceeds `snapshotTotalBonus` (350 000 000) by 35.7 %. The `safeTransfer` in `claimSurvived` reverts because the contract holds only `snapshotTotalBonus` in bonus tokens. The staker's principal is also locked because the claim path is the only exit after resolution. [6](#0-5) [7](#0-6)

### Citations

**File:** src/ConfidencePool.sol (L259-260)
```text
        sumStakeTime += contribTime;
        sumStakeTimeSq += contribTimeSq;
```

**File:** src/ConfidencePool.sol (L677-685)
```text
    function _clampUserSums(address u) internal {
        uint256 start = riskWindowStart;
        uint256 stake_ = eligibleStake[u];
        if (start == 0 || stake_ == 0) return;
        if (userSumStakeTime[u] < stake_ * start) {
            userSumStakeTime[u] = stake_ * start;
            userSumStakeTimeSq[u] = stake_ * start * start;
        }
    }
```

**File:** src/ConfidencePool.sol (L696-719)
```text
    function _bonusShare(address u, uint256 userEligible) internal view returns (uint256) {
        if (snapshotTotalBonus == 0) return 0;
        // No observable risk → no bonus (see contract natspec).
        if (riskWindowStart == 0) return 0;
        uint256 T = outcomeFlaggedAt;

        // Underflow guards on both subtractions: globally the sum of squares is nonneg, but
        // truncation/rounding pathologies could push individual terms over.
        uint256 userPlus = T * T * userEligible + userSumStakeTimeSq[u];
        uint256 userMinus = 2 * T * userSumStakeTime[u];
        uint256 userScore = userPlus > userMinus ? userPlus - userMinus : 0;

        uint256 plus = T * T * snapshotTotalStaked + snapshotSumStakeTimeSq;
        uint256 minus = 2 * T * snapshotSumStakeTime;
        uint256 globalScore = plus > minus ? plus - minus : 0;

        if (globalScore == 0) {
            // No time elapsed in the risk window for anyone → fallback to amount-weighted.
            if (snapshotTotalStaked == 0) return 0;
            return Math.mulDiv(userEligible, snapshotTotalBonus, snapshotTotalStaked);
        }
        // mulDiv handles the final multiply-then-divide via 512-bit intermediates, so a very
        // large `snapshotTotalBonus` cannot push the numerator over uint256 before division.
        return Math.mulDiv(userScore, snapshotTotalBonus, globalScore);
```

**File:** src/ConfidencePool.sol (L814-815)
```text
        sumStakeTime = totalEligibleStake * t;
        sumStakeTimeSq = totalEligibleStake * t * t;
```
