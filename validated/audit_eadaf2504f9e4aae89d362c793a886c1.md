Looking at the scoped contracts for an analog to the missing pause-guard vulnerability class.

**Key observations from `ConfidencePool.sol`:**

- `stake()` (line 222) has `whenPoolNotPaused` ✓
- `contributeBonus()` (line 266) has `whenPoolNotPaused` ✓
- `withdraw()` (line 288) has **no** `whenPoolNotPaused` ✗

`withdraw()` moves funds (transfers `eligibleStake[msg.sender]` back to the caller) and is callable by any staker, yet it is not gated by the pause modifier. The DESIGN.md (§9) documents the withdraw lifecycle in terms of registry-state gating only — it says nothing about pause behavior for `withdraw()`. The inconsistency is not documented as intentional anywhere in the three mandatory documents.

---

### Title
Missing `whenPoolNotPaused` on `withdraw()` allows stake withdrawal while pool is paused — (File: src/ConfidencePool.sol)

### Summary
`ConfidencePool.withdraw()` lacks the `whenPoolNotPaused` modifier that guards `stake()` and `contributeBonus()`. Any staker can withdraw their eligible stake while the pool is paused, defeating the purpose of the pause mechanism in critical-scenario responses.

### Finding Description
`stake()` and `contributeBonus()` both carry `whenPoolNotPaused`, signalling that the pause is intended to halt user-initiated fund movements during critical scenarios (exploits, upgrades, active bugs). `withdraw()` at line 288 carries only `nonReentrant` — no pause guard — so a staker can call it successfully while the pool is paused. The function transfers the caller's full `eligibleStake` balance and zeroes out their accounting entries, which is a material state change and fund movement that the pause is supposed to prevent.

### Impact Explanation
If the owner pauses the pool in response to a discovered vulnerability in the withdrawal path (e.g., an accounting bug in `_clampUserSums`, `sumStakeTime`, or `sumStakeTimeSq` manipulation), stakers can still call `withdraw()` and drain their stake before the issue is remediated. The owner has no mechanism to halt withdrawals short of a contract upgrade, because `pause()` does not cover `withdraw()`. This undermines the entire purpose of the pause: an emergency stop that only freezes deposits while leaving fund-exit open is not a meaningful emergency stop.

### Likelihood Explanation
The pool owner must have paused the pool AND a staker must act during the pause window. Pausing is an emergency-only action, so the precondition is uncommon. However, the scenario is realistic: a bug discovered post-deployment that affects withdrawal accounting is exactly the class of issue that motivates a pause, and any staker (unprivileged) can exploit the window without any special access.

### Recommendation
Add `whenPoolNotPaused` to `withdraw()`, consistent with `stake()` and `contributeBonus()`:

```solidity
- function withdraw() external nonReentrant {
+ function withdraw() external nonReentrant whenPoolNotPaused {
```

### Proof of Concept
1. Owner discovers a critical accounting bug and calls `pause()`.
2. Pool is now paused; `stake()` and `contributeBonus()` revert with `PoolPaused`.
3. Staker calls `withdraw()` — the function has no pause check, so it proceeds.
4. `eligibleStake[staker]` is transferred out and zeroed; global accumulators are decremented.
5. The owner's emergency pause has not prevented the fund movement it was intended to halt. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** src/ConfidencePool.sol (L167-170)
```text
    modifier whenPoolNotPaused() {
        if (paused()) revert PoolPaused();
        _;
    }
```

**File:** src/ConfidencePool.sol (L222-222)
```text
    function stake(uint256 amount) external nonReentrant whenPoolNotPaused {
```

**File:** src/ConfidencePool.sol (L266-266)
```text
    function contributeBonus(uint256 amount) external nonReentrant whenPoolNotPaused {
```

**File:** src/ConfidencePool.sol (L288-288)
```text
    function withdraw() external nonReentrant {
```
