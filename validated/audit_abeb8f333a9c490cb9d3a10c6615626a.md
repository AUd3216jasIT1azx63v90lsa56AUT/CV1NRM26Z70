### Title
Factory Access Controls Bypassed via Direct `ConfidencePool.initialize()` Call, Enabling Rogue Pool with Attacker-Controlled Moderator and Recovery Address - (File: src/ConfidencePool.sol)

### Summary
`ConfidencePoolFactory.createPool()` enforces three critical guards — `whenNotPaused`, `allowedStakeToken`, and `UnauthorizedCreator` — before deploying and initializing a pool clone. Because `ConfidencePool.initialize()` is `external initializer` with no caller restriction, any actor can deploy a clone of the public `poolImplementation` address and call `initialize()` directly, bypassing all three factory-level controls. The most dangerous consequence is that the attacker can supply themselves as both `outcomeModerator_` and `recoveryAddress_`, giving them full control over outcome flagging and the CORRUPTED sweep destination for any valid agreement.

### Finding Description
`ConfidencePoolFactory.createPool()` applies three guards before initializing a clone:

1. `whenNotPaused` — blocks pool creation during factory emergencies.
2. `allowedStakeToken[stakeToken]` — prevents fee-on-transfer / rebasing tokens.
3. `IAgreement(agreement).owner() != msg.sender` → `UnauthorizedCreator` — ensures only the agreement owner can create pools for their agreement. [1](#0-0) 

`ConfidencePool.initialize()` is declared `external initializer` with no restriction on who may call it: [2](#0-1) 

The `poolImplementation` address is a public state variable on the factory, readable by anyone: [3](#0-2) 

An attacker can therefore:
1. Read `factory.poolImplementation()`.
2. Deploy their own clone via `Clones.clone(poolImplementation)` (no factory involvement required).
3. Call `initialize()` directly, passing:
   - Any `stakeToken_` (including fee-on-transfer tokens not on the allowlist).
   - Any `agreement_` that passes `isAgreementValid` (they need not own it).
   - Themselves as `outcomeModerator_` — the address that calls `flagOutcome`.
   - Themselves as `recoveryAddress_` — the CORRUPTED sweep destination.
   - Themselves as `owner_`.

`initialize()` only validates non-zero addresses, expiry bounds, minStake, and registry agreement validity — none of the factory's three guards: [4](#0-3) 

### Impact Explanation
The most severe path exploits the `outcomeModerator_` and `recoveryAddress_` parameters together:

- As the self-appointed moderator, the attacker can call `flagOutcome(CORRUPTED, false, address(0))` (bad-faith) the moment the registry reaches `CORRUPTED`, regardless of whether the breach is in-scope for this pool.
- `claimCorrupted()` then sweeps the entire pool — all staked principal plus bonus — to `recoveryAddress`, which is the attacker's own address. [5](#0-4) 

Even without a CORRUPTED registry event, the attacker as owner controls `recoveryAddress` and receives all unclaimed bonus via `sweepUnclaimedBonus()`. The `allowedStakeToken` bypass additionally allows a fee-on-transfer token to be used, causing every `safeTransfer` on claim/withdraw to deliver less than the recorded `eligibleStake`, silently under-paying stakers. [6](#0-5) 

### Likelihood Explanation
The `poolImplementation` address is public and the OpenZeppelin `Clones` library is standard. Deploying a clone and calling `initialize()` requires no special privilege — only knowledge of a valid agreement address (publicly discoverable from the registry). The attack is fully permissionless. Stakers who do not verify that a pool appears in `factory.getPoolsByAgreement(agreement)` before depositing are exposed. Given that the factory's pool list is the only canonical discovery mechanism and is not enforced on-chain, social-engineering or UI-level confusion is a realistic vector.

### Recommendation
Add a factory-only caller check inside `ConfidencePool.initialize()`. The simplest approach is to store the deploying factory address in an immutable (set in the constructor of the implementation) and revert if `msg.sender != factory`:

```solidity
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

Alternatively, replicate the three factory guards (`allowedStakeToken`, agreement-owner check, and paused state) inside `initialize()` itself, accepting the registry call overhead.

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {ConfidencePool} from "src/ConfidencePool.sol";
import {ConfidencePoolFactory} from "src/ConfidencePoolFactory.sol";

contract RoguePoolAttack {
    function exploit(
        address factory,
        address validAgreement,
        address anyToken,          // e.g. fee-on-transfer token not on allowlist
        address safeHarborRegistry,
        address[] calldata accounts
    ) external returns (address roguePool) {
        // 1. Read the public implementation address — no privilege needed.
        address impl = ConfidencePoolFactory(factory).poolImplementation();

        // 2. Deploy a clone without going through the factory.
        roguePool = Clones.clone(impl);

        // 3. Initialize with attacker as owner, moderator, AND recoveryAddress.
        //    No allowedStakeToken check, no UnauthorizedCreator check, no whenNotPaused.
        ConfidencePool(roguePool).initialize(
            validAgreement,
            anyToken,
            safeHarborRegistry,
            address(this),   // outcomeModerator_ = attacker
            block.timestamp + 31 days,
            1,
            address(this),   // recoveryAddress_ = attacker
            address(this),   // owner_ = attacker
            accounts
        );

        // 4. Advertise roguePool to stakers. Once they deposit and the registry
        //    reaches CORRUPTED, call flagOutcome(CORRUPTED, false, address(0))
        //    then claimCorrupted() to sweep the full pool to address(this).
    }
}
``` [7](#0-6) [8](#0-7)

### Citations

**File:** src/ConfidencePoolFactory.sol (L24-24)
```text
    address public poolImplementation;
```

**File:** src/ConfidencePoolFactory.sol (L67-113)
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

**File:** src/ConfidencePool.sol (L474-508)
```text
    function sweepUnclaimedBonus() external nonReentrant {
        if (outcome != PoolStates.Outcome.SURVIVED && outcome != PoolStates.Outcome.EXPIRED) {
            revert OutcomeNotEligibleForSweep();
        }

        // Reserve principal still owed to non-claimers plus any bonus they're entitled to. When
        // `riskWindowStart == 0` (no observable risk), `_bonusShare` returns 0 for everyone, so
        // the bonus is not owed to any staker and the entire snapshotTotalBonus is sweepable.
        uint256 reserved;
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

        // Bonus is only unreserved when no staker is owed it (no risk window, or no stakers left).
        // In that case the sweep removes it from the pool, so drop it from the live `totalBonus`
        // too — keeping the accounting honest for any later re-snapshot. Clamp to `totalBonus` so
        // swept donations/dust (never counted in it) can't over-decrement or underflow.
        if (totalEligibleStake == 0 || riskWindowStart == 0) {
            totalBonus -= amount <= totalBonus ? amount : totalBonus;
        }

        // Intentionally does NOT set claimsStarted. A direct-transfer donation of as little as 1
        // wei would otherwise let anyone flip the flag post-flagOutcome and block the moderator's
        // documented pre-claim re-flag window. Genuine reliance only comes from claim entrypoints.
        stakeToken.safeTransfer(recoveryAddress, amount);

        emit BonusSwept(msg.sender, recoveryAddress, amount);
```
