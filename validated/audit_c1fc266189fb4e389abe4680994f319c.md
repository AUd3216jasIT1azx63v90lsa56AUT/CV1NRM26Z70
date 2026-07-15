### Title
`stake()` minStake check ignores existing eligible stake, blocking legitimate top-ups - (File: src/ConfidencePool.sol)

### Summary
The `stake()` function in `ConfidencePool.sol` checks whether the new deposit `amount` alone meets `minStake`, without accounting for the caller's existing `eligibleStake[msg.sender]`. A staker whose current position already exceeds `minStake` is blocked from adding any incremental amount smaller than `minStake`, even though the resulting total would remain well above the minimum.

### Finding Description
`stake()` enforces the minimum at two points:

```solidity
// src/ConfidencePool.sol L224
if (amount < minStake) revert BelowMinStake();
```

and after the balance-diff transfer check:

```solidity
// src/ConfidencePool.sol L242
if (received < minStake) revert BelowMinStake();
```

Neither check considers `eligibleStake[msg.sender]`. The check is purely on the size of the new deposit, not on the staker's resulting total position.

Concrete scenario:
- `minStake` = 10,000 tokens
- Alice calls `stake(10_000e18)` → succeeds; `eligibleStake[Alice]` = 10,000
- Alice later calls `stake(5_000e18)` to increase her position to 15,000
- Both checks revert with `BelowMinStake` because `5,000 < 10,000`, even though Alice's post-deposit total (15,000) would be well above the minimum

This is the exact root cause described in the external report, mapped to `stake()` / `eligibleStake` instead of `deposit()` / `balanceOf`.

### Impact Explanation
Any staker with an existing position above `minStake` is permanently unable to top up their stake by less than `minStake`. This breaks the incremental staking use case: a staker who wants to increase their risk exposure (and thus their k=2 bonus weight) by a modest amount is forced to either stake a full `minStake` again or not stake at all. Given that `minStake` is set at pool initialization and could be a large value, this is a meaningful functional restriction on legitimate staker behavior. No funds are at risk, but the staking interface is materially broken for the top-up case.

### Likelihood Explanation
Any staker who has already met `minStake` and wishes to add a smaller incremental amount will hit this. This is a normal and expected usage pattern (dollar-cost averaging into a position, adding capital as the risk window approaches). The likelihood is medium-high: it affects every staker who attempts a top-up below `minStake`, which is a common pattern in staking protocols.

### Recommendation
Include the caller's existing `eligibleStake` in both checks:

```solidity
function stake(uint256 amount) external nonReentrant whenPoolNotPaused {
    if (amount == 0) revert InvalidAmount();
    // Include existing stake so top-ups are not blocked when total >= minStake
    if (eligibleStake[msg.sender] + amount < minStake) revert BelowMinStake();
    ...
    uint256 received = ...;
    if (received == 0) revert NoTokensReceived();
    if (eligibleStake[msg.sender] + received < minStake) revert BelowMinStake();
    ...
}
```

This mirrors the fix applied in the referenced DegenLockToken commit: the guard should enforce a minimum *total* position, not a minimum *per-deposit* amount.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

// Assume pool is deployed with minStake = 10_000e18
// Alice stakes the minimum successfully
pool.stake(10_000e18); // succeeds, eligibleStake[Alice] = 10_000e18

// Alice wants to add 5_000e18 more (total would be 15_000e18 > minStake)
pool.stake(5_000e18);
// REVERTS: BelowMinStake — because 5_000e18 < 10_000e18 (minStake)
// even though eligibleStake[Alice] + 5_000e18 = 15_000e18 > minStake
```

The revert occurs at [1](#0-0)  (pre-transfer check) and again at [2](#0-1)  (post-transfer balance-diff check), both of which compare only the new deposit against `minStake` without reading `eligibleStake[msg.sender]`. [3](#0-2)

### Citations

**File:** src/ConfidencePool.sol (L83-83)
```text
    mapping(address staker => uint256 amount) public eligibleStake;
```

**File:** src/ConfidencePool.sol (L224-224)
```text
        if (amount < minStake) revert BelowMinStake();
```

**File:** src/ConfidencePool.sol (L242-242)
```text
        if (received < minStake) revert BelowMinStake();
```
