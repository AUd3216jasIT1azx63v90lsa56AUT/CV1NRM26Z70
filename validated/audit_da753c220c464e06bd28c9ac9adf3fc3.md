### Title
Named Attacker Contract Locked Out of `claimAttackerBounty`, Forfeiting Entire Bounty to `recoveryAddress` — (File: `src/ConfidencePool.sol`)

### Summary
`claimAttackerBounty()` gates execution on `msg.sender == attacker`, but the payout destination is already hardcoded to the stored `attacker` address. If the named whitehat is a smart contract that cannot itself initiate the call (e.g., a DAO treasury, multisig, or vesting contract without a path to call arbitrary external functions), the bounty is permanently unclaimable. After the 180-day `corruptedClaimDeadline`, `sweepUnclaimedCorrupted()` permissionlessly redirects the entire pool — stakers' principal plus the full bonus — to `recoveryAddress`, giving the whitehat nothing.

### Finding Description
In `claimAttackerBounty()`, the guard at line 436 requires `msg.sender == attacker`. The transfer at line 449 sends funds to `attacker` regardless of who the caller is, so the `msg.sender` restriction is a pure access gate with no economic purpose — the beneficiary is already fixed in storage.

The `attacker` address is set by the moderator in `flagOutcome()` at line 356 based on off-chain identity. Whitehats frequently operate through smart-contract wallets: DAOs, multisigs, or vesting contracts. If the named `attacker` is any such contract that lacks a code path to call `claimAttackerBounty()` on the pool, the 180-day window elapses with `bountyClaimed < bountyEntitlement`, and `sweepUnclaimedCorrupted()` (lines 456–471) becomes callable by anyone, transferring the full pool balance to `recoveryAddress`.

The moderator cannot remedy this after `claimsStarted` is latched (which `claimAttackerBounty` itself sets on first payout), and the re-flag window closes on the first value-moving claim. If no claim ever succeeds, the moderator could theoretically re-flag, but only before `claimsStarted` — and `sweepUnclaimedCorrupted` also sets `claimsStarted`, permanently foreclosing any correction.

### Impact Explanation
The named whitehat attacker loses `bountyEntitlement = snapshotTotalStaked + snapshotTotalBonus` — the entire pool — to `recoveryAddress`. This is a complete, irreversible loss of the bounty for the legitimate beneficiary. The sponsor (who controls `recoveryAddress`) receives funds they are not entitled to under the good-faith CORRUPTED resolution path.

### Likelihood Explanation
The moderator names the attacker from their BattleChain identity, which may resolve to a smart-contract address. DAOs, multisigs (Gnosis Safe), and vesting contracts are standard operating structures for security researchers and whitehat teams. Any such contract without an explicit `claimAttackerBounty(pool)` execution path is affected. The 180-day window is long but finite, and the attacker may not even be aware of the pool's existence or the specific function signature required.

### Recommendation
Remove the `msg.sender != attacker` guard and allow any caller to trigger the bounty disbursement, with the payout still going to the stored `attacker` address. The beneficiary is already fixed in state; the caller identity carries no economic meaning. This mirrors the exact fix applied in the referenced Graph protocol pull request: derive the recipient from stored state rather than from `msg.sender`.

```solidity
function claimAttackerBounty() external nonReentrant {
    if (outcome != PoolStates.Outcome.CORRUPTED) revert OutcomeNotSet();
    if (bountyClaimed == bountyEntitlement) revert BountyAlreadyClaimed();
    if (!goodFaith) revert InvalidGoodFaithParams();
-   if (msg.sender != attacker) revert NotAttacker();
    if (block.timestamp > corruptedClaimDeadline) revert ClaimWindowExpired();
    ...
    stakeToken.safeTransfer(attacker, payout); // beneficiary unchanged
}
```

### Proof of Concept
1. Moderator calls `flagOutcome(CORRUPTED, true, daoContract)` where `daoContract` is a DAO treasury or multisig that has no execution path to call `claimAttackerBounty()` on the pool.
2. `bountyEntitlement` is set to `snapshotTotalStaked + snapshotTotalBonus` (the full pool).
3. `daoContract` cannot call `claimAttackerBounty()` — every attempt from any EOA reverts `NotAttacker` at line 436; every attempt from `daoContract` itself is impossible because the contract has no such code path.
4. 180 days elapse; `block.timestamp > corruptedClaimDeadline`.
5. Any address calls `sweepUnclaimedCorrupted()`. The check `block.timestamp <= corruptedClaimDeadline` passes (deadline has expired), and the entire pool balance is transferred to `recoveryAddress`.
6. The whitehat `daoContract` receives zero despite being the named beneficiary of the good-faith CORRUPTED resolution.

The relevant code: [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** src/ConfidencePool.sol (L354-362)
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
```

**File:** src/ConfidencePool.sol (L432-453)
```text
    function claimAttackerBounty() external nonReentrant {
        if (outcome != PoolStates.Outcome.CORRUPTED) revert OutcomeNotSet();
        if (bountyClaimed == bountyEntitlement) revert BountyAlreadyClaimed();
        if (!goodFaith) revert InvalidGoodFaithParams();
        if (msg.sender != attacker) revert NotAttacker();
        if (block.timestamp > corruptedClaimDeadline) revert ClaimWindowExpired();

        uint256 remaining = bountyEntitlement - bountyClaimed;
        // aderyn-fp-next-line(reentrancy-state-change)
        uint256 freeBalance = stakeToken.balanceOf(address(this));
        uint256 payout = remaining <= freeBalance ? remaining : freeBalance;

        uint256 newBountyClaimed = bountyClaimed + payout;
        bountyClaimed = newBountyClaimed;
        if (payout > 0) {
            corruptedReserve -= payout;
            if (!claimsStarted) claimsStarted = true;
            stakeToken.safeTransfer(attacker, payout);
        }

        emit AttackerBountyClaimed(attacker, payout, newBountyClaimed, bountyEntitlement);
    }
```

**File:** src/ConfidencePool.sol (L456-471)
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
    }
```
