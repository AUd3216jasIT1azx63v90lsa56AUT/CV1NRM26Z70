### Title
Push-Pattern Claim Functions Permanently Lock Blacklisted Staker Funds - (File: src/ConfidencePool.sol)

### Summary
`claimSurvived()` and `claimExpired()` use a push-based `safeTransfer(msg.sender, payout)` pattern. If a staker's address is blacklisted by the stake token after depositing, every claim attempt reverts and the staker's principal plus bonus share are permanently locked in the contract with no recovery path.

### Finding Description
Both `claimSurvived()` and `claimExpired()` deliver the staker's payout by pushing tokens directly to `msg.sender`:

- `claimSurvived()` line 403: `stakeToken.safeTransfer(msg.sender, payout)`
- `claimExpired()` line 601: `stakeToken.safeTransfer(msg.sender, payout)`

`SafeERC20.safeTransfer` reverts on any transfer failure. If the staker's address is blacklisted by the stake token (e.g., USDC or USDT — both standard ERC20 tokens eligible for the factory allowlist), the call reverts and the entire transaction is rolled back. Because `hasClaimed[msg.sender]` is set to `true` and `eligibleStake[msg.sender]` is deleted only within the same atomic transaction that includes the transfer, the revert restores those values — but the staker is left in a state where they can never successfully complete the claim: every future attempt hits the same blacklist revert.

The factory allowlist comment in `ConfidencePoolFactory.sol` (lines 27–31) explicitly excludes only fee-on-transfer and rebasing tokens. It says nothing about tokens with user-level blacklisting. `DESIGN.md §12` acknowledges only the scenario where "the pool's own balance" is frozen (i.e., the pool contract itself is blacklisted), not the case where an individual staker's address is blacklisted. These are distinct scenarios: a user-blacklisted token still allows the pool to transfer to all other stakers normally, so the factory allowlist provides no defense here.

`sweepUnclaimedBonus()` (lines 483–488) reserves `totalEligibleStake` — which includes the blacklisted staker's amount — so the stuck funds cannot be swept to `recoveryAddress` either. They are permanently immobilized.

### Impact Explanation
A staker whose address is blacklisted by the stake token after depositing permanently loses their full principal plus their proportional bonus share. The funds remain in the contract but are inaccessible to the staker (claim reverts), inaccessible to the recovery address (`sweepUnclaimedBonus` reserves them), and inaccessible to any other party. This is an irrecoverable loss of user funds with no on-chain remedy.

### Likelihood Explanation
USDC and USDT are the most common ERC20 stake tokens and both implement address-level blacklists. Regulatory blacklisting of individual addresses is a documented, real-world occurrence. The factory allowlist does not screen for this property, so any allowlisted blacklistable token creates this exposure. A staker need only be blacklisted at any point between their deposit and their post-resolution claim call for the loss to materialize.

### Recommendation
Replace the push pattern in `claimSurvived()` and `claimExpired()` with a pull pattern: record the owed amount in a `pendingClaims` mapping when the staker calls the claim function (burning their `eligibleStake` and updating global accounting at that point), then provide a separate `withdrawClaim(address recipient)` function that transfers the recorded amount to a caller-specified address. This decouples share-burning from token delivery, allowing a blacklisted staker to wait for the blacklist to be lifted or to direct funds to an unblacklisted address they control.

### Proof of Concept
1. Factory owner allowlists USDC as a stake token (standard ERC20, passes the allowlist check at `ConfidencePoolFactory.sol` line 77).
2. Alice calls `stake(1000e6)` — 1000 USDC deposited; `eligibleStake[Alice] = 1000e6`.
3. The pool resolves `SURVIVED`; `flagOutcome` sets `outcome = SURVIVED`.
4. Alice's address is added to USDC's blacklist (e.g., regulatory freeze).
5. Alice calls `claimSurvived()`:
   - Lines 391–400 execute (state updates in memory).
   - Line 403: `stakeToken.safeTransfer(Alice, payout)` → USDC reverts `"Blacklisted"`.
   - Entire transaction reverts; `hasClaimed[Alice]` stays `false`, `eligibleStake[Alice]` stays `1000e6`.
6. Alice retries — same revert every time. No alternative claim entrypoint exists.
7. Anyone calls `sweepUnclaimedBonus()`:
   - Line 484: `reserved = totalEligibleStake` (includes Alice's 1000e6).
   - `freeBalance - reserved = 0` (or only sweeps genuine excess/donations).
   - Alice's 1000 USDC is never swept; it is permanently locked in the contract. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** src/ConfidencePool.sol (L391-403)
```text
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

**File:** src/ConfidencePool.sol (L482-488)
```text
        uint256 reserved;
        if (totalEligibleStake != 0) {
            reserved = totalEligibleStake;
            if (riskWindowStart != 0) {
                reserved += snapshotTotalBonus - claimedBonus;
            }
        }
```

**File:** src/ConfidencePool.sol (L589-601)
```text
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

**File:** src/ConfidencePoolFactory.sol (L27-31)
```text
    /// default. Pools assume a standard ERC20 (no transfer fees, no rebasing); fee-on-transfer
    /// tokens silently under-pay every claim/withdraw, and fee-on-sender or negative-rebasing
    /// tokens erode the pool balance below tracked liabilities and permanently lock later claims.
    /// Checked only at `createPool` time, so de-listing a token does not affect existing pools.
    mapping(address token => bool allowed) public override allowedStakeToken;
```
