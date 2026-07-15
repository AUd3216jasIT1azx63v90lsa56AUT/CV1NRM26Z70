Looking at the external report's vulnerability class — a function callable by anyone that bypasses validation enforced by the intended entry point — I need to check whether `ConfidencePool.initialize()` can be called directly, bypassing the factory's validation.

### Title
`ConfidencePool.initialize()` Callable Directly, Bypassing Factory's Moderator and Token Allowlist Guards — (File: src/ConfidencePool.sol)

### Summary
`ConfidencePool.initialize()` has no access control restricting it to `ConfidencePoolFactory`. Any caller can deploy a minimal proxy clone of the pool implementation and call `initialize()` directly, bypassing the factory's `allowedStakeToken` allowlist and, critically, the hardcoded `defaultOutcomeModerator` assignment. This lets an attacker self-appoint as `outcomeModerator` and `owner`, then drain all staker funds once the underlying agreement reaches `CORRUPTED` state.

### Finding Description
`ConfidencePoolFactory.createPool()` enforces three guards that `ConfidencePool.initialize()` does not:

1. **Token allowlist** — `allowedStakeToken[stakeToken]` (factory line 77) blocks fee-on-transfer and rebasing tokens. `initialize()` accepts any non-zero address.
2. **Agreement ownership** — `IAgreement(agreement).owner() == msg.sender` (factory line 82) ensures only the agreement owner creates a pool. `initialize()` has no such check.
3. **Trusted moderator** — the factory hardcodes `defaultOutcomeModerator` (factory line 95) as the `outcomeModerator_` argument. `initialize()` accepts any non-zero address for `outcomeModerator_`.

Because `initialize()` carries only the `initializer` modifier (preventing re-initialization, not restricting the caller), an attacker can:

```
clone = Clones.clone(poolImplementation);
IConfidencePool(clone).initialize(
    legitimateAgreement,   // valid agreement they do not own
    anyToken,              // non-allowlisted token
    safeHarborRegistry,
    attacker,              // attacker becomes outcomeModerator
    expiry,
    minStake,
    attacker,              // attacker becomes recoveryAddress
    attacker,              // attacker becomes owner
    accounts
);
``` [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
With the attacker as `outcomeModerator`, once the legitimate agreement reaches `CORRUPTED` registry state (a real-world event the attacker cannot manufacture but can wait for), the attacker calls:

```
flagOutcome(PoolStates.Outcome.CORRUPTED, true, attacker);
```

This sets `bountyEntitlement = snapshotTotalStaked + snapshotTotalBonus` — the entire pool — and names the attacker as the whitehat. The attacker then calls `claimAttackerBounty()`, which transfers the full pool balance to themselves. All staker principal and bonus are stolen. [4](#0-3) [5](#0-4) [6](#0-5) 

Even without a CORRUPTED event, the attacker as `owner` can set `recoveryAddress` to themselves at any time before resolution, redirecting all CORRUPTED sweeps. [7](#0-6) 

### Likelihood Explanation
The attacker only needs to: (a) deploy a minimal proxy clone (a standard, permissionless operation using OpenZeppelin `Clones`), (b) call `initialize()` with attacker-controlled parameters, and (c) attract stakers to the rogue pool. The pool can be made to look legitimate by referencing a real, valid agreement address — `initialize()` validates `isAgreementValid` but not agreement ownership. The rogue pool will not appear in the factory's `_poolsByAgreement` registry, but off-chain advertisement is sufficient. Likelihood is medium given the social-engineering requirement.

### Recommendation
Add a factory-only guard to `initialize()`. The simplest approach is to store the factory address in the implementation and enforce it:

```solidity
address public factory;

function initialize(...) external initializer {
    factory = msg.sender; // set once; factory is the deployer
    // existing checks...
}
```

Or pass the factory address as a constructor argument to the implementation and check `msg.sender == factory` at the top of `initialize()`. Alternatively, add an `onlyFactory` modifier analogous to the `onlyRouter` pattern recommended in the external report. [8](#0-7) 

### Proof of Concept
```solidity
// Attacker deploys a rogue pool bypassing the factory
address clone = Clones.clone(poolImplementation);

IConfidencePool(clone).initialize(
    legitimateAgreement,   // real agreement, attacker does not own it
    anyERC20,              // non-allowlisted token
    address(safeHarborRegistry),
    attacker,              // attacker is now outcomeModerator
    block.timestamp + 31 days,
    1e18,
    attacker,              // attacker is recoveryAddress
    attacker,              // attacker is owner
    scopeAccounts
);

// Stakers stake into the rogue pool (advertised off-chain as legitimate)
// ...

// When the agreement legitimately reaches CORRUPTED state:
IConfidencePool(clone).flagOutcome(
    PoolStates.Outcome.CORRUPTED,
    true,
    attacker   // attacker named as whitehat
);

// Attacker drains the entire pool (all staker principal + bonus)
IConfidencePool(clone).claimAttackerBounty();
``` [9](#0-8) [10](#0-9)

### Citations

**File:** src/ConfidencePool.sol (L157-159)
```text
    constructor() Ownable(msg.sender) {
        _disableInitializers();
    }
```

**File:** src/ConfidencePool.sol (L179-219)
```text
    function initialize(
        address agreement_,
        address stakeToken_,
        address safeHarborRegistry_,
        address outcomeModerator_,
        uint256 expiry_,
        uint256 minStake_,
        address recoveryAddress_,
        address owner_,
        address[] calldata accounts
    ) external initializer {
        if (agreement_ == address(0)) revert ZeroAddress();
        if (stakeToken_ == address(0)) revert ZeroAddress();
        if (safeHarborRegistry_ == address(0)) revert ZeroAddress();
        if (outcomeModerator_ == address(0)) revert ZeroAddress();
        if (owner_ == address(0)) revert ZeroAddress();
        if (recoveryAddress_ == address(0)) revert InvalidRecoveryAddress();
        if (expiry_ < block.timestamp + _MIN_EXPIRY_LEAD) revert ExpiryTooSoon();
        if (expiry_ > type(uint32).max) revert ExpiryTooFar();
        if (minStake_ == 0) revert InvalidAmount();
        // aderyn-fp-next-line(reentrancy-state-change)
        if (!IBattleChainSafeHarborRegistry(safeHarborRegistry_).isAgreementValid(agreement_)) {
            revert InvalidAgreement();
        }

        agreement = agreement_;
        stakeToken = IERC20(stakeToken_);
        safeHarborRegistry = IBattleChainSafeHarborRegistry(safeHarborRegistry_);
        outcomeModerator = outcomeModerator_;
        // forge-lint: disable-next-line(unsafe-typecast)
        expiry = uint32(expiry_);
        minStake = minStake_;
        recoveryAddress = recoveryAddress_;
        outcome = PoolStates.Outcome.UNRESOLVED;

        _replaceScope(accounts);

        // Direct assignment (skipping Ownable2Step's two-step) so no `owner() == initializer-caller`
        // window exists between init and the new owner accepting. Two-step still applies to later transfers.
        _transferOwnership(owner_);
    }
```

**File:** src/ConfidencePool.sol (L322-327)
```text
    function flagOutcome(PoolStates.Outcome newOutcome, bool goodFaith_, address attacker_) external onlyModerator {
        // Re-flag allowed pre-claim so the moderator can fix a typo'd outcome / attacker before
        // any participant locks in the wrong distribution. The window closing on the FIRST claim
        // (`claimsStarted`) is by design — a value-movement finality latch, not a front-runnable
        // moderator privilege. See docs/DESIGN.md (re-flag window).
        if (outcome != PoolStates.Outcome.UNRESOLVED && claimsStarted) revert OutcomeAlreadySet();
```

**File:** src/ConfidencePool.sol (L361-362)
```text
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

**File:** src/ConfidencePool.sol (L611-618)
```text
    function setRecoveryAddress(address newRecoveryAddress) external onlyOwner {
        if (newRecoveryAddress == address(0)) revert InvalidRecoveryAddress();

        address oldRecoveryAddress = recoveryAddress;
        recoveryAddress = newRecoveryAddress;

        emit RecoveryAddressUpdated(oldRecoveryAddress, newRecoveryAddress);
    }
```

**File:** src/ConfidencePoolFactory.sol (L67-101)
```text
    function createPool(
        address agreement,
        address stakeToken,
        uint256 expiry,
        uint256 minStake,
        address recoveryAddress,
        address[] calldata accounts
    ) external whenNotPaused returns (address pool) {
        if (agreement == address(0) || stakeToken == address(0)) revert ZeroAddress();
        if (recoveryAddress == address(0)) revert ZeroAddress();
        if (!allowedStakeToken[stakeToken]) revert StakeTokenNotAllowed();
        if (expiry < block.timestamp + _MIN_EXPIRY_LEAD) revert ExpiryTooSoon();
        // aderyn-fp-next-line(reentrancy-state-change)
        if (!safeHarborRegistry.isAgreementValid(agreement)) revert InvalidAgreement();
        // aderyn-fp-next-line(reentrancy-state-change)
        if (IAgreement(agreement).owner() != msg.sender) revert UnauthorizedCreator();

        // Salt incorporates the per-agreement index so an agreement can back many pools while
        // keeping deterministic, collision-free clone addresses.
        bytes32 salt = keccak256(abi.encode(agreement, _poolsByAgreement[agreement].length));
        pool = Clones.cloneDeterministic(poolImplementation, salt);

        // aderyn-fp-next-line(reentrancy-state-change)
        IConfidencePool(pool)
            .initialize(
                agreement,
                stakeToken,
                address(safeHarborRegistry),
                defaultOutcomeModerator,
                expiry,
                minStake,
                recoveryAddress,
                msg.sender,
                accounts
            );
```
