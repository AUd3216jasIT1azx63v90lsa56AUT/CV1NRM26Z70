After reviewing both contracts and the DESIGN.md, I can identify a direct analog.

**Key observation:** Every setter in `ConfidencePoolFactory` emits an event on update — `SafeHarborRegistryUpdated`, `PoolImplementationUpdated`, `DefaultOutcomeModeratorUpdated`, `StakeTokenAllowedUpdated` — but `initialize()` sets the same three critical state variables silently, with no event emission. [1](#0-0) [2](#0-1) 

---

### Title
Missing Event Emission in `ConfidencePoolFactory.initialize()` Leaves Initial Critical Configuration Unobservable Off-Chain — (File: src/ConfidencePoolFactory.sol)

### Summary
`ConfidencePoolFactory.initialize()` sets three critical state variables — `safeHarborRegistry`, `poolImplementation`, and `defaultOutcomeModerator` — without emitting any events. Every corresponding setter function emits a dedicated event for the same variables. The initial factory configuration is therefore invisible to off-chain indexers, monitoring systems, and clients that rely solely on event logs.

### Finding Description
In `ConfidencePoolFactory.initialize()` (lines 50–64), the function writes:

```solidity
safeHarborRegistry = IBattleChainSafeHarborRegistry(safeHarborRegistry_);
poolImplementation = poolImplementation_;
defaultOutcomeModerator = defaultOutcomeModerator_;
```

No event is emitted for any of these assignments. By contrast, the three setter functions that update the same variables each emit a dedicated event:

- `setSafeHarborRegistry` → `SafeHarborRegistryUpdated(old, newSafeHarborRegistry)`
- `setPoolImplementation` → `PoolImplementationUpdated(old, newPoolImplementation)`
- `setDefaultOutcomeModerator` → `DefaultOutcomeModeratorUpdated(old, newDefaultOutcomeModerator)`

The `__Ownable_init(msg.sender)` call emits `OwnershipTransferred(address(0), msg.sender)`, but nothing records the initial registry, implementation, or moderator addresses in the event log.

`ConfidencePool.initialize()` is partially mitigated: the factory's `PoolCreated` event (lines 104–113) re-emits `stakeToken`, `expiry`, `minStake`, `recoveryAddress`, `outcomeModerator`, and `safeHarborRegistry` for each clone. No equivalent factory-level "initialized" event exists for the factory's own configuration.

### Impact Explanation
`poolImplementation` is the clone template for every pool ever deployed by this factory. `defaultOutcomeModerator` is the address that gains the power to flag outcomes (SURVIVED / CORRUPTED) for every pool. `safeHarborRegistry` is the root-of-trust for agreement validation. An off-chain client reconstructing factory state from logs alone cannot determine the values these variables held at deployment — only subsequent `set*` calls are visible. This breaks event-log-based auditing, monitoring dashboards, and any tooling that tracks the factory's configuration history from genesis.

### Likelihood Explanation
Certain: `initialize()` is called exactly once per deployment. Every factory deployment silently sets these three variables. There is no workaround short of reading storage directly.

### Recommendation
Emit the existing events (or a dedicated `FactoryInitialized` event) inside `initialize()` for the initial values of `safeHarborRegistry`, `poolImplementation`, and `defaultOutcomeModerator`, consistent with what the setter functions already emit:

```solidity
emit SafeHarborRegistryUpdated(address(0), safeHarborRegistry_);
emit PoolImplementationUpdated(address(0), poolImplementation_);
emit DefaultOutcomeModeratorUpdated(address(0), defaultOutcomeModerator_);
```

### Proof of Concept
1. Deploy `ConfidencePoolFactory` and call `initialize(registry, impl, moderator)`.
2. Query all events emitted by the transaction.
3. Observe: only `OwnershipTransferred(address(0), deployer)` is present; no `SafeHarborRegistryUpdated`, `PoolImplementationUpdated`, or `DefaultOutcomeModeratorUpdated` event is emitted.
4. Call `setPoolImplementation(newImpl)` in a subsequent transaction.
5. Observe: `PoolImplementationUpdated(impl, newImpl)` is emitted — the update is visible, but the original `impl` value set at initialization is not recorded anywhere in the log.

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
