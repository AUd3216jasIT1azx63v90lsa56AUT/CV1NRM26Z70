### Title
Aggregate check in `_clampUserSums` fails to detect pre-risk deposits when mixed with post-risk deposits, inflating attacker's bonus share - (File: src/ConfidencePool.sol)

### Summary
`_clampUserSums` uses a single aggregate inequality to decide whether a user's per-deposit sums need to be floored to `riskWindowStart`. When a user holds both pre-risk and post-risk deposits, the post-risk deposits' higher timestamps can compensate for the pre-risk deposits' lower timestamps in the aggregate, causing the check to pass even though the pre-risk deposit was never clamped. The result is that the user's `userSumStakeTime` / `userSumStakeTimeSq` remain stale (encoding a wall-clock entry time earlier than `riskWindowStart`), inflating their k=2 bonus score at the expense of honest stakers.

### Finding Description
`_clampUserSums` is the lazy per-user counterpart to the eager global reset performed in `_markRiskWindowStart`. Its purpose is to rewrite a user's accumulated sums as if every pre-risk deposit entered at `riskWindowStart`, matching the global treatment.

The detection condition is:

```solidity
if (userSumStakeTime[u] < stake_ * start) {
    userSumStakeTime[u] = stake_ * start;
    userSumStakeTimeSq[u] = stake_ * start * start;
}
``` [1](#0-0) 

`stake_ * start` is the value `userSumStakeTime` *would* have if every token entered at `riskWindowStart`. The check assumes this is a lower bound on the current value only when all deposits pre-date `riskWindowStart`. That assumption breaks when the user also has post-risk deposits.

**Concrete scenario:**

| Event | amount | wall-clock entry | `userSumStakeTime` after |
|---|---|---|---|
| Stake 1 (pre-risk) | A₁ | t₁ < S | A₁·t₁ |
| `riskWindowStart` set to S | — | — | global reset; per-user still A₁·t₁ |
| Stake 2 (post-risk) | A₂ | t₂ > S | A₁·t₁ + A₂·t₂ |

At claim time `_clampUserSums` checks:

```
A₁·t₁ + A₂·t₂  <  (A₁+A₂)·S
```

Rearranging: `A₁·(t₁−S) < A₂·(S−t₂)`. Since t₁ < S and t₂ > S both sides are negative, so the inequality is equivalent to `A₁·(S−t₁) > A₂·(t₂−S)`. Whenever the post-risk deposit's "surplus" `A₂·(t₂−S)` is ≥ the pre-risk deposit's "deficit" `A₁·(S−t₁)`, the check does **not** trigger and the pre-risk deposit is never clamped. [2](#0-1) 

The global sums were correctly reset at `_markRiskWindowStart` — they encode A₁·S for the pre-risk deposit. [3](#0-2) 

But the per-user sums still encode A₁·t₁ (t₁ < S). When `_bonusShare` computes:

```
userScore = T²·eligibleStake[u] − 2T·userSumStakeTime[u] + userSumStakeTimeSq[u]
``` [4](#0-3) 

it uses the stale t₁ instead of S, producing a larger `(T − t₁)²` term than the correct `(T − S)²`. The global denominator uses the correctly clamped sums, so the user's share of the bonus pool is overstated.

### Impact Explanation
An attacker who deliberately stakes before `riskWindowStart` and then stakes again after it opens (with a post-risk amount large enough to satisfy the aggregate check) will have their pre-risk deposit's entry time treated as t₁ < S in the bonus numerator while the global denominator correctly uses S. This inflates their k=2 score and steals bonus from every other staker. The magnitude scales with how far t₁ precedes S and the relative deposit sizes. The stolen bonus comes directly from honest stakers' entitlements; principal is unaffected.

### Likelihood Explanation
The condition is reachable by any unprivileged staker with no special access. Staking before and after `riskWindowStart` is a normal usage pattern (the protocol explicitly allows staking during `UNDER_ATTACK`). The attacker only needs to size the post-risk deposit so that `A₂·(t₂−S) ≥ A₁·(S−t₁)`, which is straightforward to compute on-chain before submitting the second stake. No trusted role is required.

### Recommendation
Replace the aggregate inequality with a per-deposit-aware check. The simplest correct approach is to track the timestamp of the last clamp (or a `sumsClamped` flag per user) and re-clamp unconditionally whenever `riskWindowStart` has advanced past the stored marker. Alternatively, store each deposit's entry time individually and compute the user score by iterating deposits — though that changes the O(1) claim complexity. A minimal fix is to record `lastClampedAt[u]` and clamp whenever `riskWindowStart > lastClampedAt[u]`:

```solidity
mapping(address => uint32) public lastClampedAt;

function _clampUserSums(address u) internal {
    uint256 start = riskWindowStart;
    uint256 stake_ = eligibleStake[u];
    if (start == 0 || stake_ == 0) return;
    if (lastClampedAt[u] < start) {          // per-user marker, not aggregate sum
        userSumStakeTime[u] = stake_ * start;
        userSumStakeTimeSq[u] = stake_ * start * start;
        lastClampedAt[u] = uint32(start);
    }
}
```

This mirrors the external report's recommended fix: validate each component (here, each user's clamp epoch) rather than an aggregate that can mask individual violations.

### Proof of Concept
```
Setup:
  riskWindowStart S = 2000
  T (outcomeFlaggedAt) = 4000
  totalBonus = 1000 tokens

Attacker:
  Stake 1: amount=100 at t1=1000  → userSumStakeTime=100000, userSumStakeTimeSq=1e8
  [riskWindowStart set to 2000; global sums eagerly reset]
  Stake 2: amount=100 at t2=3000  → userSumStakeTime=400000, userSumStakeTimeSq=1e9

_clampUserSums check at claim:
  stake_ * start = 200 * 2000 = 400000
  userSumStakeTime = 400000
  400000 < 400000 → FALSE → clamp does NOT trigger

Attacker's bonus score (stale sums):
  userScore = 4000²×200 − 2×4000×400000 + 1e9
            = 3.2e9 − 3.2e9 + 1e9 = 1,000,000,000

Correct score (if clamped: userSumStakeTime=500000, userSumStakeTimeSq=1.3e9):
  userScore = 3.2e9 − 2×4000×500000 + 1.3e9
            = 3.2e9 − 4e9 + 1.3e9 = 500,000,000

Attacker receives 2× the bonus they are entitled to, stealing from other stakers.
```

### Citations

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

**File:** src/ConfidencePool.sol (L704-706)
```text
        uint256 userPlus = T * T * userEligible + userSumStakeTimeSq[u];
        uint256 userMinus = 2 * T * userSumStakeTime[u];
        uint256 userScore = userPlus > userMinus ? userPlus - userMinus : 0;
```

**File:** src/ConfidencePool.sol (L813-815)
```text
        // touching that user.
        sumStakeTime = totalEligibleStake * t;
        sumStakeTimeSq = totalEligibleStake * t * t;
```
