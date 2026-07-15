### Title
`contributeBonus` blocked by staking-specific guard during `PROMOTION_REQUESTED`, preventing legitimate bonus deposits - (File: src/ConfidencePool.sol)

### Summary

`contributeBonus` applies `_assertDepositsAllowed`, a guard whose entire rationale is preventing late-join gaming of the k=2 staking formula. Bonus contributors are not stakers, do not participate in the k=2 formula, and cannot game any distribution metric by contributing during `PROMOTION_REQUESTED`. The check is overly broad: it blocks a legitimate, harmless action for the same reason the DSCEngine liquidator's health-factor check blocked a liquidation that did not affect the liquidator's own position.

### Finding Description

`contributeBonus` calls `_assertDepositsAllowed(_observePoolState())`, which reverts with `StakingClosed` when the registry is in `PROMOTION_REQUESTED`, `PRODUCTION`, or `CORRUPTED`. [1](#0-0) 

The function's only state effect is `totalBonus += received`. [2](#0-1) 

`_assertDepositsAllowed` was designed exclusively around staking timing: its natspec and DESIGN.md §3 explain that blocking `PROMOTION_REQUESTED` stops a staker from entering in the closing-window stretch and gaming the k=2 score `amount × (T − entryTime)²`. [3](#0-2) [4](#0-3) 

A bonus contributor:
- Receives no `eligibleStake` entry
- Contributes nothing to `userSumStakeTime`, `userSumStakeTimeSq`, `sumStakeTime`, or `sumStakeTimeSq`
- Earns no bonus share at resolution
- Cannot manipulate any staker's k=2 score by contributing at any time

There is therefore no gaming vector that `_assertDepositsAllowed` protects against when called from `contributeBonus`. The guard is the exact structural analog of `_revertIfHealthFactorIsBroken(msg.sender)` in the DSCEngine liquidator: a check designed for one actor's position being incorrectly applied to a different actor whose action does not affect that position.

### Impact Explanation

During `PROMOTION_REQUESTED`, any call to `contributeBonus` reverts. A sponsor or third party who wishes to top up the bonus pool in the final stretch of the risk window — for example, to increase the incentive for stakers who are still locked in — is permanently blocked from doing so. The bonus pool is frozen at whatever level it reached before the registry entered `PROMOTION_REQUESTED`. This is a loss of intended protocol functionality: `contributeBonus` is documented as permissionless and is economically required for rational staker participation, yet it is silently closed during a normal lifecycle state.

### Likelihood Explanation

`PROMOTION_REQUESTED` is a standard, expected registry state in every pool's lifecycle. Sponsors have a direct economic incentive to add bonus during this period (to reward stakers who remained committed through the risk window). The revert is silent from the caller's perspective — the error `StakingClosed` gives no indication that the block is staking-specific, making the failure non-obvious. Any sponsor or keeper attempting a bonus top-up during this window will be blocked.

### Recommendation

Remove `_assertDepositsAllowed` from `contributeBonus`. The existing guards — `outcome != UNRESOLVED` and `block.timestamp >= expiry` — are sufficient to close the contribution window at the correct lifecycle boundary. The staking-specific timing guard has no valid purpose in the bonus-contribution path.

```solidity
function contributeBonus(uint256 amount) external nonReentrant whenPoolNotPaused {
    if (amount == 0) revert InvalidAmount();
    if (outcome != PoolStates.Outcome.UNRESOLVED) revert OutcomeAlreadySet();
    if (block.timestamp >= expiry) revert StakingClosed();

-   _assertDepositsAllowed(_observePoolState());

    // Balance-diff defense-in-depth — see `stake`.
    ...
}
```

If `_observePoolState` side-effects (scope lock, risk window sealing) are still desired on bonus contributions, call it directly without routing through `_assertDepositsAllowed`.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

// Assume pool is deployed and registry transitions to PROMOTION_REQUESTED.
// Sponsor attempts to top up the bonus pool.

// Registry is now PROMOTION_REQUESTED (riskWindowStart sealed, riskWindowEnd imminent).
// outcome == UNRESOLVED, block.timestamp < expiry.

vm.prank(sponsor);
stakeToken.approve(address(pool), 1_000e18);

// Reverts with StakingClosed — _assertDepositsAllowed blocks PROMOTION_REQUESTED.
// Sponsor cannot add bonus even though their action has zero effect on k=2 scores.
vm.expectRevert(IConfidencePool.StakingClosed.selector);
pool.contributeBonus(1_000e18);

// Meanwhile, stake() is correctly blocked for the same state (late-join prevention).
// contributeBonus() should NOT share this restriction — it has no k=2 timing exposure.
```

### Citations

**File:** src/ConfidencePool.sol (L266-271)
```text
    function contributeBonus(uint256 amount) external nonReentrant whenPoolNotPaused {
        if (amount == 0) revert InvalidAmount();
        if (outcome != PoolStates.Outcome.UNRESOLVED) revert OutcomeAlreadySet();
        if (block.timestamp >= expiry) revert StakingClosed();

        _assertDepositsAllowed(_observePoolState());
```

**File:** src/ConfidencePool.sol (L282-282)
```text
        totalBonus += received;
```

**File:** src/ConfidencePool.sol (L722-733)
```text
    /// @dev `UNDER_ATTACK` is intentionally NOT blocked while `PROMOTION_REQUESTED` is. Both are
    /// active-risk (attackable, still corruptible); the asymmetry is about deposit *timing*, not
    /// safety — UNDER_ATTACK deposits earn ~zero k=2 bonus and self-lock (no trap), whereas
    /// PROMOTION_REQUESTED is the closing-window stretch where a late join would be gameable. See
    /// docs/DESIGN.md (deposit gating).
    function _assertDepositsAllowed(IAttackRegistry.ContractState state) private pure {
        if (
            state == IAttackRegistry.ContractState.PROMOTION_REQUESTED
                || state == IAttackRegistry.ContractState.PRODUCTION || state == IAttackRegistry.ContractState.CORRUPTED
        ) {
            revert StakingClosed();
        }
```

**File:** docs/DESIGN.md (L63-67)
```markdown
- **`PROMOTION_REQUESTED`:** the agreement has requested to exit toward `PRODUCTION`, so the risk
  window is about to close (`riskWindowEnd` imminent). Blocking deposits here stops a late join
  in the final stretch — entering with almost no remaining risk-bearing time before resolution.
  The state is still attackable; it is the *closing window*, not assured survival, that justifies
  the block.
```
