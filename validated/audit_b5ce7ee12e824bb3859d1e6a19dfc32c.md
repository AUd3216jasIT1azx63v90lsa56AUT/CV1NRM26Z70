### Title
Factory Stake-Token Allowlist and Creator Authorization Bypassed via Direct Clone Initialization — (File: src/ConfidencePool.sol)

### Summary
`ConfidencePool.initialize()` is `external` with only the `initializer` modifier, which only prevents re-initialization but imposes no caller restriction. Anyone can deploy a clone of `poolImplementation` directly and call `initialize()` with arbitrary parameters, bypassing the two critical factory-enforced guards: the stake-token allowlist and the agreement-owner authorization check.

### Finding Description
`ConfidencePoolFactory.createPool()` enforces two security checks before deploying a pool:

```solidity
// src/ConfidencePoolFactory.sol lines 77-82
if (!allowedStakeToken[stakeToken]) revert StakeTokenNotAllowed();
...
if (IAgreement(agreement).owner() != msg.sender) revert UnauthorizedCreator();
``` [1](#0-0) 

`ConfidencePool.initialize()` is declared with no caller restriction:

```solidity
// src/ConfidencePool.sol line 179
function initialize(...) external initializer {
``` [2](#0-1) 

The `initializer` modifier (from the non-upgradeable `@openzeppelin/contracts/proxy/utils/Initializable.sol`) only prevents re-initialization on the same contract instance. A freshly deployed clone has `_initialized == 0` in its own storage, so any caller can initialize it. An attacker can therefore:

1. Call `Clones.clone(poolImplementation)` to deploy a new clone (the implementation address is public via `poolImplementation`).
2. Call `initialize()` on the clone with arbitrary parameters — including a non-allowlisted stake token, themselves as `outcomeModerator_` and `owner_`, and any agreement they do not own.

The resulting pool is fully functional but:
- Not registered in `_poolsByAgreement`, so it is invisible to `getPoolsByAgreement()` / `poolCountByAgreement()`.
- Created with a token the factory explicitly prohibits.
- Controlled by the attacker as both owner and moderator. [3](#0-2) 

### Impact Explanation
The factory's own comment on the allowlist states: *"fee-on-transfer tokens silently under-pay every claim/withdraw, and fee-on-sender or negative-rebasing tokens erode the pool balance below tracked liabilities and permanently lock later claims."* [4](#0-3) 

A pool initialized with such a token will have permanently broken accounting: `eligibleStake` and `totalEligibleStake` will diverge from the actual token balance on every `stake()` call (the balance-diff check catches the discrepancy but does not prevent the pool from being created), and later `claimSurvived` / `claimExpired` transfers will revert or under-pay, locking staker principal. The attacker-as-moderator can also flag any outcome arbitrarily, redirecting all funds to `recoveryAddress` (which the attacker also controls as owner).

### Likelihood Explanation
Exploiting this to harm stakers requires social engineering: stakers must be directed to an unregistered pool. The factory's `PoolCreated` event and `getPoolsByAgreement()` are the canonical discovery paths, so a vigilant staker who verifies factory registration is not at risk. Likelihood is **low**, but the bypass is trivially executable by any unprivileged caller and the impact on deceived stakers is total loss of principal.

### Recommendation
Add a factory-only guard to `initialize()`. The simplest approach is to store the deploying factory address in the implementation and check it at initialization time:

```solidity
// In ConfidencePool.sol
address public immutable factory;

constructor(address factory_) Ownable(msg.sender) {
    factory = factory_;
    _disableInitializers();
}

function initialize(...) external initializer {
    if (msg.sender != factory) revert NotFactory();
    ...
}
```

Because `factory` is `immutable`, it is baked into every clone's bytecode at deployment time (clones share the implementation's code), so no extra storage slot is needed and the check costs a single `CALLER` + comparison.

### Proof of Concept

```solidity
// Anyone can run this — no agreement ownership required
address poolImpl = factory.poolImplementation(); // public getter

// 1. Deploy a clone directly, bypassing the factory entirely
address rogue = Clones.clone(poolImpl);

// 2. Initialize with a fee-on-transfer token not on the allowlist
//    and with the attacker as owner + moderator + recoveryAddress
IConfidencePool(rogue).initialize(
    legitimateAgreement,   // any registry-valid agreement
    feeOnTransferToken,    // NOT in allowedStakeToken — bypassed
    address(safeHarborRegistry),
    attacker,              // attacker is outcomeModerator
    block.timestamp + 31 days,
    1,
    attacker,              // attacker is recoveryAddress
    attacker,              // attacker is owner
    scopeAccounts
);

// 3. Stakers lured to `rogue` stake normally; fee-on-transfer token
//    causes eligibleStake to diverge from actual balance.
//    Attacker calls flagOutcome(CORRUPTED, false, address(0)) and
//    then claimCorrupted() to sweep all funds to attacker address.
//    Alternatively, broken token accounting permanently locks principal.
``` [5](#0-4) [6](#0-5)

### Citations

**File:** src/ConfidencePoolFactory.sol (L26-35)
```text
    /// @notice Tokens the factory owner has approved for use as a pool's stake token. Empty by
    /// default. Pools assume a standard ERC20 (no transfer fees, no rebasing); fee-on-transfer
    /// tokens silently under-pay every claim/withdraw, and fee-on-sender or negative-rebasing
    /// tokens erode the pool balance below tracked liabilities and permanently lock later claims.
    /// Checked only at `createPool` time, so de-listing a token does not affect existing pools.
    mapping(address token => bool allowed) public override allowedStakeToken;
    /// @notice All pools created for a given agreement, in creation order. An agreement may back
    /// many pools — each commits to its own (locked) scope, so duplicates are allowed and
    /// curated off-chain.
    mapping(address agreement => address[] pools) internal _poolsByAgreement;
```

**File:** src/ConfidencePoolFactory.sol (L67-114)
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

        _poolsByAgreement[agreement].push(pool);
        emit PoolCreated(
            agreement,
            pool,
            stakeToken,
            expiry,
            minStake,
            recoveryAddress,
            defaultOutcomeModerator,
            address(safeHarborRegistry)
        );
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
