### Title
Staker principal permanently locked when staker address is blacklisted by ERC20 token after withdrawal window closes - (File: src/ConfidencePool.sol)

### Summary
Once `riskWindowStart` is sealed, `withdraw()` is permanently disabled. All claim paths (`claimSurvived`, `claimExpired`) transfer exclusively to `msg.sender` with no alternative recipient. If a staker's address is subsequently blacklisted by the stake token (e.g., USDC), their principal is irrecoverably locked: the claim reverts, and `sweepUnclaimedBonus` explicitly reserves the principal of non-claimers and cannot sweep it.

### Finding Description
The vulnerability has two cooperating components:

**1. Claims are hardwired to `msg.sender` with no alternative recipient.**

`claimSurvived` and `claimExpired` both unconditionally transfer to the caller's own address: [1](#0-0) [2](#0-1) 

There is no mechanism for a staker to designate a different recipient, and no admin or moderator function can redirect a specific staker's principal to an alternative address.

**2. `sweepUnclaimedBonus` permanently reserves the blacklisted staker's principal.**

`sweepUnclaimedBonus` computes a `reserved` amount that includes the full `totalEligibleStake` of all non-claimers, and only sweeps the balance *above* that reserve: [3](#0-2) 

`totalEligibleStake` is only decremented inside the claim paths (`claimSurvived`, `claimExpired`). Because a blacklisted staker can never successfully execute a claim, their share of `totalEligibleStake` is never decremented, so the reserve never shrinks to expose their principal as sweepable. The funds are permanently trapped.

**3. The withdrawal escape hatch is one-way closed.**

`withdraw()` is permanently disabled once `riskWindowStart != 0`: [4](#0-3) 

A staker who deposited before the risk window opened has no exit path once the window seals, even if their address is later blacklisted.

**Why this is not covered by DESIGN.md §12:** That section states "A true stuck-state would require a defective stake token that freezes the pool's own balance, which the factory allowlist exists to exclude." The scenario here is distinct: the pool's own balance is not frozen; only a specific staker's *address* is blacklisted. USDC is a standard ERC20 token that would legitimately appear on the factory allowlist, and its per-address blacklist does not make it "defective" in the fee-on-transfer or rebasing sense the allowlist is designed to exclude.

### Impact Explanation
A staker's entire principal is permanently locked in the pool contract with no recovery path. The funds cannot be claimed by the staker (transfer reverts), cannot be swept by `sweepUnclaimedBonus` (reserved by `totalEligibleStake`), and cannot be redirected by any privileged actor. This is a material, irrecoverable loss of the staker's deposited capital.

### Likelihood Explanation
The attack requires a stake token with a per-address blacklist (USDC is the canonical example and a natural candidate for the factory allowlist), and an attacker who dusts the staker's address with funds from a known exploit, triggering Circle's blacklisting policy. The staker must have staked before the risk window opened (so their withdrawal window is already closed). This is a targeted but realistic attack: a motivated adversary who wants to sabotage a specific staker's recovery after the pool resolves SURVIVED can execute it entirely off-chain (by sending tainted tokens to the victim's address) while the pool is in-flight.

### Recommendation
Add a staker-controlled "claim-to" address mechanism, allowing a staker to pre-register an alternative recipient before the claim is executed. For example:

```solidity
mapping(address staker => address claimRecipient) public claimRecipient;

function setClaimRecipient(address recipient) external {
    if (recipient == address(0)) revert ZeroAddress();
    claimRecipient[msg.sender] = recipient;
}
```

Then in `claimSurvived` and `claimExpired`, resolve the actual transfer target as:
```solidity
address to = claimRecipient[msg.sender] != address(0) ? claimRecipient[msg.sender] : msg.sender;
stakeToken.safeTransfer(to, payout);
```

This preserves the self-custody default while giving stakers a path to redirect funds if their primary address becomes compromised or blacklisted.

### Proof of Concept
1. Alice stakes 10,000 USDC in the pool while the registry is in `NEW_DEPLOYMENT`.
2. The registry transitions to `UNDER_ATTACK`; `riskWindowStart` is sealed. Alice's `withdraw()` is now permanently disabled. [4](#0-3) 
3. An attacker sends a small amount of USDC obtained from a recent exploit to Alice's address on the same chain.
4. Circle blacklists Alice's address on the USDC contract.
5. The registry transitions to `PRODUCTION`; the moderator calls `flagOutcome(SURVIVED, ...)`.
6. Alice calls `claimSurvived()`. The function reaches `stakeToken.safeTransfer(msg.sender, payout)` and reverts because Alice's address is blacklisted. [1](#0-0) 
7. `hasClaimed[Alice]` remains `false`; `eligibleStake[Alice]` and `totalEligibleStake` are never decremented.
8. Anyone calls `sweepUnclaimedBonus()`. The function computes `reserved = totalEligibleStake` (which still includes Alice's 10,000 USDC) and finds `freeBalance - reserved == 0` (or only dust), so it reverts with `NothingToSweep`. [3](#0-2) 
9. Alice's 10,000 USDC is permanently locked in the pool with no on-chain recovery path.

### Citations

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

**File:** src/ConfidencePool.sol (L402-404)
```text
        if (!claimsStarted) claimsStarted = true;
        stakeToken.safeTransfer(msg.sender, payout);
        emit ClaimSurvived(msg.sender, userEligible, bonusShare);
```

**File:** src/ConfidencePool.sol (L483-493)
```text
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

**File:** src/ConfidencePool.sol (L599-601)
```text

        if (!claimsStarted) claimsStarted = true;
        stakeToken.safeTransfer(msg.sender, payout);
```
