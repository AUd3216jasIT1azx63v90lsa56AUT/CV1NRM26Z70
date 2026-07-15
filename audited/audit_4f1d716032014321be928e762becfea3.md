### Title
Missing Event Emission in `initialize` for Sensitive State Variables - (File: src/ConfidencePoolFactory.sol)

### Summary
`ConfidencePoolFactory.initialize` sets three sensitive state variables — `safeHarborRegistry`, `poolImplementation`, and `defaultOutcomeModerator` — without emitting any events, while every corresponding post-deploy setter (`setSafeHarborRegistry`, `setPoolImplementation`, `setDefaultOutcomeModerator`) does emit a dedicated event. Off-chain indexers that reconstruct factory configuration purely from event logs will have no record of the initial values.

### Finding Description
`ConfidencePoolFactory.initialize` (lines 50–64) assigns:

```solidity
safeHarborRegistry = IBattleChainSafeHarborRegistry(safeHarborRegistry_);
poolImplementation = poolImplementation_;
defaultOutcomeModerator = defaultOutcomeModerator_;
```

without emitting `SafeHarborRegistryUpdated`, `PoolImplementationUpdated`, or `DefaultOutcomeModeratorUpdated`. The three setter functions that handle post-deploy mutations of the same variables each emit the corresponding event:

- `setSafeHarborRegistry` → `emit SafeHarborRegistryUpdated(old, newSafeHarborRegistry)` (line 132)
- `setPoolImplementation` → `emit PoolImplementationUpdated(old, newPoolImplementation)` (line 141)
- `setDefaultOutcomeModerator` → `emit DefaultOutcomeModeratorUpdated(old, newDefaultOutcomeModerator)` (line 150)

The `initialize` call emits only the inherited `OwnershipTransferred` event from `__Ownable_init`. No factory-level event records the initial registry address, pool implementation, or default moderator.

### Impact Explanation
Off-chain monitoring systems, subgraphs, and security dashboards that rely on event logs to reconstruct the factory's configuration will have a blind spot for the genesis state. Any tool that tracks `SafeHarborRegistryUpdated` to know which registry the factory trusts, or `PoolImplementationUpdated` to know which implementation clones are based on, will start from an empty baseline and miss the initial values entirely. This is particularly relevant for `defaultOutcomeModerator`, which is the entity that controls outcome flagging (and therefore fund distribution) for every pool the factory creates.

### Likelihood Explanation
The factory is deployed once and `initialize` is called exactly once. Every off-chain consumer that subscribes to the factory's event stream from block 0 will miss the initial values. The likelihood of this causing a monitoring gap is high because the pattern is consistent: all three setters emit events, creating a reasonable expectation that the initial assignment also emits events.

### Recommendation
Emit the three events at the end of `initialize`, mirroring the setter pattern with a zero-address `old` value to signal genesis assignment:

```solidity
emit SafeHarborRegistryUpdated(address(0), safeHarborRegistry_);
emit PoolImplementationUpdated(address(0), poolImplementation_);
emit DefaultOutcomeModeratorUpdated(address(0), defaultOutcomeModerator_);
```

### Proof of Concept

1. Deploy `ConfidencePoolFactory` and call `initialize(registry, impl, moderator)`.
2. Query all events emitted by the factory from the deployment block onward.
3. Observe: only `OwnershipTransferred` is present; no `SafeHarborRegistryUpdated`, `PoolImplementationUpdated`, or `DefaultOutcomeModeratorUpdated` events exist.
4. Call `setSafeHarborRegistry(newRegistry)` — `SafeHarborRegistryUpdated(oldRegistry, newRegistry)` is emitted.
5. An indexer replaying events from step 2 onward sees `oldRegistry` in step 4 but has no prior event establishing what `oldRegistry` was — the initial value is invisible to the event log. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** src/ConfidencePoolFactory.sol (L50-64)
```text
    function initialize(address safeHarborRegistry_, address poolImplementation_, address defaultOutcomeModerator_)
        external
        initializer
    {
        if (safeHarborRegistry_ == address(0)) revert ZeroAddress();
        if (poolImplementation_ == address(0)) revert ZeroAddress();
        if (defaultOutcomeModerator_ == address(0)) revert ZeroAddress();

        __Ownable_init(msg.sender);
        __Pausable_init();

        safeHarborRegistry = IBattleChainSafeHarborRegistry(safeHarborRegistry_);
        poolImplementation = poolImplementation_;
        defaultOutcomeModerator = defaultOutcomeModerator_;
    }
```

**File:** src/ConfidencePoolFactory.sol (L128-151)
```text
    function setSafeHarborRegistry(address newSafeHarborRegistry) external onlyOwner {
        if (newSafeHarborRegistry == address(0)) revert ZeroAddress();
        address old = address(safeHarborRegistry);
        safeHarborRegistry = IBattleChainSafeHarborRegistry(newSafeHarborRegistry);
        emit SafeHarborRegistryUpdated(old, newSafeHarborRegistry);
    }

    /// @inheritdoc IConfidencePoolFactory
    // aderyn-ignore-next-line(centralization-risk)
    function setPoolImplementation(address newPoolImplementation) external onlyOwner {
        if (newPoolImplementation == address(0)) revert ZeroAddress();
        address old = poolImplementation;
        poolImplementation = newPoolImplementation;
        emit PoolImplementationUpdated(old, newPoolImplementation);
    }

    /// @inheritdoc IConfidencePoolFactory
    // aderyn-ignore-next-line(centralization-risk)
    function setDefaultOutcomeModerator(address newDefaultOutcomeModerator) external onlyOwner {
        if (newDefaultOutcomeModerator == address(0)) revert ZeroAddress();
        address old = defaultOutcomeModerator;
        defaultOutcomeModerator = newDefaultOutcomeModerator;
        emit DefaultOutcomeModeratorUpdated(old, newDefaultOutcomeModerator);
    }
```

**File:** src/interfaces/IConfidencePoolFactory.sol (L15-18)
```text
    event SafeHarborRegistryUpdated(address indexed oldRegistry, address indexed newRegistry);
    event PoolImplementationUpdated(address indexed oldImplementation, address indexed newImplementation);
    event DefaultOutcomeModeratorUpdated(address indexed oldModerator, address indexed newModerator);
    event StakeTokenAllowedUpdated(address indexed token, bool allowed);
```
