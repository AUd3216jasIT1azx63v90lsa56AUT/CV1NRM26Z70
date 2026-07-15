### Title
Sponsor Can Silently Redirect All Staker Funds via Unrestricted `setRecoveryAddress` After Withdrawals Are Permanently Disabled - (File: src/ConfidencePool.sol)

### Summary
`setRecoveryAddress` carries no post-stake lock, allowing a malicious pool sponsor to change the CORRUPTED sweep destination to an arbitrary address after stakers are permanently locked in. Stakers commit capital against a known `recoveryAddress`, but the sponsor can silently redirect it at any point before `claimCorrupted` executes, draining the entire pool.

### Finding Description
`setExpiry` is explicitly protected by an `expiryLocked` latch that fires on the first stake, with the stated rationale of protecting "staker reliance." No equivalent protection exists for `recoveryAddress`:

```solidity
// setExpiry — correctly locked after first stake
function setExpiry(uint256 newExpiry) external onlyOwner {
    if (expiryLocked) revert ExpiryLocked();   // ← guard exists
    ...
}

// setRecoveryAddress — no analogous guard
function setRecoveryAddress(address newRecoveryAddress) external onlyOwner {
    if (newRecoveryAddress == address(0)) revert InvalidRecoveryAddress();
    recoveryAddress = newRecoveryAddress;       // ← no lock check
    emit RecoveryAddressUpdated(...);
}
``` [1](#0-0) [2](#0-1) 

`recoveryAddress` is the sole destination for all funds under bad-faith CORRUPTED and under the permissionless auto-CORRUPTED backstop. `claimCorrupted` reads `recoveryAddress` live at sweep time: [3](#0-2) 

Once `riskWindowStart != 0`, `withdraw()` is permanently disabled via a one-way latch: [4](#0-3) 

The sponsor can therefore change `recoveryAddress` after stakers are irrevocably committed, with no on-chain mechanism to prevent or detect it before resolution executes.

### Impact Explanation
A malicious sponsor can drain the entire pool — all staker principal plus all bonus — without moderator collusion:

1. Sponsor creates a pool with a legitimate `recoveryAddress` to attract stakers.
2. Stakers deposit; `expiryLocked` flips true.
3. Registry enters `UNDER_ATTACK`; `riskWindowStart` is sealed; `withdraw()` is permanently disabled for all stakers.
4. Sponsor calls `setRecoveryAddress(sponsorAddress)` — succeeds with no revert.
5. Registry reaches `CORRUPTED`; moderator does not flag within `expiry + MODERATOR_CORRUPTED_GRACE` (180 days).
6. Anyone calls `claimExpired()` → auto-CORRUPTED branch executes, `claimsStarted = true`. [5](#0-4) 
7. Anyone calls `claimCorrupted()` → `stakeToken.safeTransfer(recoveryAddress, toSweep)` sends the full pool balance to `sponsorAddress`. [6](#0-5) 

All staker principal and bonus are permanently lost. The sponsor profits from the full pool with no privileged registry or moderator cooperation required.

### Likelihood Explanation
The sponsor is the agreement owner and is explicitly not listed as a trusted actor (only the moderator/DAO and factory owner carry that designation). The attack requires only that the registry eventually reaches `CORRUPTED` and the moderator is absent for 180 days post-expiry — both plausible conditions for a pool created by a malicious sponsor who controls the agreement. The sponsor can plan the attack from pool creation, and the `setRecoveryAddress` call is a single, cheap, unrestricted transaction that emits an event most stakers will not monitor.

### Recommendation
Add a post-stake immutability guard to `setRecoveryAddress`, mirroring the `expiryLocked` pattern already used for `setExpiry`:

```solidity
function setRecoveryAddress(address newRecoveryAddress) external onlyOwner {
    if (expiryLocked) revert RecoveryAddressLocked(); // lock after first stake
    if (newRecoveryAddress == address(0)) revert InvalidRecoveryAddress();
    address oldRecoveryAddress = recoveryAddress;
    recoveryAddress = newRecoveryAddress;
    emit RecoveryAddressUpdated(oldRecoveryAddress, newRecoveryAddress);
}
```

Alternatively, lock on `riskWindowStart != 0` (the point at which withdrawals close and stakers are irrevocably committed) if pre-stake mutability is desired for operational reasons.

### Proof of Concept

```solidity
// 1. Sponsor creates pool with recoveryAddress = legitimateAddress
// 2. Stakers call stake(amount); expiryLocked = true
// 3. Registry transitions to UNDER_ATTACK
//    → _markRiskWindowStart() seals riskWindowStart
//    → withdraw() now reverts WithdrawsDisabled for all stakers
// 4. Sponsor calls:
pool.setRecoveryAddress(sponsorAddress); // no revert — succeeds unconditionally
// 5. Registry transitions to CORRUPTED; moderator absent
// 6. After block.timestamp >= expiry + 180 days:
pool.claimExpired();
//    → outcome = CORRUPTED, claimsStarted = true, returns early
// 7. Anyone calls:
pool.claimCorrupted();
//    → stakeToken.safeTransfer(sponsorAddress, fullBalance)
//    All staker principal + bonus transferred to sponsor
``` [1](#0-0) [7](#0-6)

### Citations

**File:** src/ConfidencePool.sol (L229-231)
```text
        if (!expiryLocked) {
            expiryLocked = true;
        }
```

**File:** src/ConfidencePool.sol (L293-300)
```text
        if (
            riskWindowStart != 0
                || (state != IAttackRegistry.ContractState.NOT_DEPLOYED
                    && state != IAttackRegistry.ContractState.NEW_DEPLOYMENT
                    && state != IAttackRegistry.ContractState.ATTACK_REQUESTED)
        ) {
            revert WithdrawsDisabled();
        }
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

**File:** src/ConfidencePool.sol (L532-554)
```text
            if (state == IAttackRegistry.ContractState.CORRUPTED && riskWindowStart != 0) {
                // Scope-blind by design: this forces CORRUPTED for any corrupted agreement, even
                // one whose breach was out-of-scope (where the moderator would have flagged
                // SURVIVED). See MODERATOR_CORRUPTED_GRACE for the trust assumption this encodes.
                // Moderator is the canonical decision-maker for CORRUPTED (only they can name
                // an attacker for the good-faith bounty path). Defer to them during the grace
                // window; after that, anyone can finalize as bad-faith CORRUPTED so funds aren't
                // trapped if the DAO becomes permanently unavailable.
                if (block.timestamp < expiry + MODERATOR_CORRUPTED_GRACE) {
                    revert AgreementCorruptedAwaitingModerator();
                }
                outcome = PoolStates.Outcome.CORRUPTED;
                outcomeFlaggedAt = riskWindowEnd;
                corruptedReserve = snapshotTotalStaked + snapshotTotalBonus;
                // Lock the outcome so the moderator can't override mechanical bad-faith CORRUPTED
                // with good-faith naming an attacker — that would redirect the full pool from
                // recoveryAddress to the named address via claimAttackerBounty.
                claimsStarted = true;
                emit OutcomeFlagged(address(0), PoolStates.Outcome.CORRUPTED, false, address(0));
                // Bad-faith CORRUPTED pays nothing to the caller; the full sweep happens via
                // claimCorrupted. Return early so the SURVIVED/EXPIRED claim flow below stays
                // dormant.
                return;
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

**File:** src/ConfidencePool.sol (L622-632)
```text
    function setExpiry(uint256 newExpiry) external onlyOwner {
        if (expiryLocked) revert ExpiryLocked();
        if (newExpiry < block.timestamp + _MIN_EXPIRY_LEAD) revert ExpiryTooSoon();
        if (newExpiry > type(uint32).max) revert ExpiryTooFar();

        uint256 oldExpiry = expiry;
        // forge-lint: disable-next-line(unsafe-typecast)
        expiry = uint32(newExpiry);

        emit ExpiryUpdated(oldExpiry, newExpiry);
    }
```
