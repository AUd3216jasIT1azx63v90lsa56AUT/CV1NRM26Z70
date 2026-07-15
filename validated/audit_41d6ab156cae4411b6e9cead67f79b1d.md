### Title
`claimAttackerBounty` emits `AttackerBountyClaimed` even when no tokens are transferred — (`File: src/ConfidencePool.sol`)

### Summary
`claimAttackerBounty` unconditionally emits `AttackerBountyClaimed` outside the `if (payout > 0)` guard. When the pool's free balance is zero, `payout` resolves to zero, no `safeTransfer` executes, yet the event fires with `amount = 0`. Off-chain indexers that rely on `AttackerBountyClaimed` to track bounty progress receive a misleading signal of a successful claim.

### Finding Description
In `claimAttackerBounty` the transfer and state-mutation are correctly gated on `payout > 0`, but the event emission sits unconditionally after that block:

```solidity
// src/ConfidencePool.sol lines 446-452
if (payout > 0) {
    corruptedReserve -= payout;
    if (!claimsStarted) claimsStarted = true;
    stakeToken.safeTransfer(attacker, payout);
}

emit AttackerBountyClaimed(attacker, payout, newBountyClaimed, bountyEntitlement);
```

When `stakeToken.balanceOf(address(this)) == 0`, `payout` is set to `0` (line 442), `bountyClaimed` is written back unchanged (`bountyClaimed + 0`), no transfer occurs, and `claimsStarted` is not set — yet `AttackerBountyClaimed` is emitted with `amount = 0`. Because `bountyClaimed` is unchanged, the entry guard `if (bountyClaimed == bountyEntitlement) revert BountyAlreadyClaimed()` still passes on the next call, so the named attacker can repeat this indefinitely, flooding the event log with zero-payout `AttackerBountyClaimed` entries.

### Impact Explanation
Off-chain scripts and indexers that listen for `AttackerBountyClaimed` to determine whether the bounty has been satisfied — and consequently whether `claimCorrupted` is unblocked — will observe spurious events with `amount = 0`. A naive indexer that counts events rather than summing `amount` or comparing `totalClaimed` against `totalEntitlement` could conclude the bounty was claimed when it was not, or could be confused by repeated zero-payout events. On-chain, `claimCorrupted`'s `MustClaimBountyFirst` guard (`bountyClaimed < bountyEntitlement`) remains intact, so no direct fund loss occurs on-chain; the harm is confined to off-chain data consumers.

### Likelihood Explanation
Triggering the zero-payout path requires `stakeToken.balanceOf(address(this)) == 0` at call time while `bountyClaimed < bountyEntitlement`. Under normal operation with standard ERC20 tokens (the only tokens the factory allowlist admits), the pool balance at `flagOutcome` equals at least `snapshotTotalStaked + snapshotTotalBonus = bountyEntitlement`, and no code path drains the pool before the bounty is satisfied in good-faith CORRUPTED mode. The likelihood of the zero-balance condition arising in practice is therefore low, but the defect is structurally present and the named attacker (the only caller permitted) can exploit it whenever the condition holds.

### Recommendation
Move the `emit` inside the `if (payout > 0)` block so the event is only emitted when a transfer actually occurs:

```solidity
if (payout > 0) {
    corruptedReserve -= payout;
    if (!claimsStarted) claimsStarted = true;
    stakeToken.safeTransfer(attacker, payout);
    emit AttackerBountyClaimed(attacker, payout, newBountyClaimed, bountyEntitlement);
}
```

### Proof of Concept
1. Moderator calls `flagOutcome(CORRUPTED, true, attackerAddr)` on a pool whose balance has been reduced to zero (e.g., via a defective-but-allowlisted token that later zeroes balances, or in a test environment).
2. `bountyEntitlement = snapshotTotalStaked + snapshotTotalBonus > 0`; `bountyClaimed = 0`.
3. Named attacker calls `claimAttackerBounty()`.
4. `freeBalance = stakeToken.balanceOf(address(this)) = 0` → `payout = 0`.
5. `bountyClaimed` is written as `0 + 0 = 0` (unchanged); no transfer; `claimsStarted` stays `false`.
6. `emit AttackerBountyClaimed(attacker, 0, 0, bountyEntitlement)` fires.
7. Attacker repeats step 3 indefinitely, each time emitting a zero-payout event.
8. An off-chain indexer counting `AttackerBountyClaimed` events concludes the bounty was claimed; a script that then calls `claimCorrupted` on-chain is correctly blocked by `MustClaimBountyFirst`, but the indexer's state is corrupted. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** src/ConfidencePool.sol (L432-437)
```text
    function claimAttackerBounty() external nonReentrant {
        if (outcome != PoolStates.Outcome.CORRUPTED) revert OutcomeNotSet();
        if (bountyClaimed == bountyEntitlement) revert BountyAlreadyClaimed();
        if (!goodFaith) revert InvalidGoodFaithParams();
        if (msg.sender != attacker) revert NotAttacker();
        if (block.timestamp > corruptedClaimDeadline) revert ClaimWindowExpired();
```

**File:** src/ConfidencePool.sol (L439-452)
```text
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
```

**File:** src/interfaces/IConfidencePool.sol (L15-17)
```text
    event AttackerBountyClaimed(
        address indexed attacker, uint256 amount, uint256 totalClaimed, uint256 totalEntitlement
    );
```
