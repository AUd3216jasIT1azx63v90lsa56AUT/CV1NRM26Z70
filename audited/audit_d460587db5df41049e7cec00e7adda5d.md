### Title
Unprotected `initialize` on `ConfidencePoolFactory` allows front-running to seize factory ownership — (File: src/ConfidencePoolFactory.sol)

### Summary
`ConfidencePoolFactory.initialize()` has no caller restriction. Any address can call it first after the UUPS proxy is deployed, becoming the permanent factory owner with full administrative control over all future pools.

### Finding Description
`ConfidencePoolFactory` is a UUPS upgradeable proxy. Its `initialize` function applies only the `initializer` modifier, which prevents re-initialization but does not restrict who may call it first. [1](#0-0) 

The function calls `__Ownable_init(msg.sender)`, permanently setting the first caller as owner. The constructor correctly calls `_disableInitializers()` on the implementation to prevent direct initialization of the logic contract, but the proxy itself is left unprotected. [2](#0-1) 

The standard UUPS deployment sequence is: (1) deploy implementation, (2) deploy proxy, (3) call `initialize` on the proxy. Between steps 2 and 3, an attacker monitoring the mempool can front-run the legitimate `initialize` call with their own parameters, seizing ownership before the deployer can act. The legitimate `initialize` call then reverts with `InvalidInitialization`.

### Impact Explanation
As factory owner, the attacker gains every privileged capability documented in the README:

- `setPoolImplementation` — redirect all future pool clones to attacker-controlled logic, enabling theft of all staker and bonus funds deposited into any subsequently created pool.
- `setDefaultOutcomeModerator` — install themselves as the default moderator for every future pool, controlling all outcome flags (SURVIVED / CORRUPTED / EXPIRED) and therefore all fund flows.
- `setStakeTokenAllowed` — allowlist malicious tokens.
- `_authorizeUpgrade` (via `upgradeToAndCall`) — upgrade the factory proxy to arbitrary bytecode.
- `pause` — permanently block pool creation. [3](#0-2) 

### Likelihood Explanation
Front-running a deployment transaction is a well-known, low-skill attack on EVM chains. The attacker only needs to observe the proxy deployment in the mempool and submit `initialize` with a higher gas price before the deployer's own `initialize` transaction is included. On BattleChain (an EVM-compatible L2), mempool visibility and transaction ordering depend on the sequencer model, but the window exists whenever deployment and initialization are separate transactions — which is the standard UUPS deployment pattern used here.

### Recommendation
Eliminate the gap between proxy deployment and initialization by using OpenZeppelin's `ERC1967Proxy` constructor, which accepts and immediately executes the `initialize` calldata atomically:

```solidity
new ERC1967Proxy(address(implementation), abi.encodeCall(factory.initialize, (registry, impl, moderator)));
```

Alternatively, add a deployer address check inside `initialize` (e.g., an immutable set in the implementation constructor) so only the intended deployer can call it.

### Proof of Concept
```solidity
// Attacker observes the proxy deployment in the mempool, then submits:
ConfidencePoolFactory(proxyAddress).initialize(
    safeHarborRegistry,   // legitimate registry (public knowledge)
    poolImplementation,   // legitimate impl (public knowledge)
    attacker              // attacker's address as defaultOutcomeModerator
);
// __Ownable_init(msg.sender) sets attacker as owner.
// Deployer's initialize() call reverts: InvalidInitialization.

// Attacker now calls:
factory.setPoolImplementation(maliciousImpl);
// All future createPool() calls deploy clones of maliciousImpl,
// which can drain staker funds on any claim/withdraw call.
```

### Citations

**File:** src/ConfidencePoolFactory.sol (L44-47)
```text
    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }
```

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

**File:** src/ConfidencePoolFactory.sol (L137-174)
```text
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

    /// @inheritdoc IConfidencePoolFactory
    // aderyn-ignore-next-line(centralization-risk)
    function setStakeTokenAllowed(address token, bool allowed) external onlyOwner {
        if (token == address(0)) revert ZeroAddress();
        allowedStakeToken[token] = allowed;
        emit StakeTokenAllowedUpdated(token, allowed);
    }

    /// @inheritdoc IConfidencePoolFactory
    // aderyn-ignore-next-line(centralization-risk)
    function pause() external onlyOwner {
        _pause();
    }

    /// @inheritdoc IConfidencePoolFactory
    // aderyn-ignore-next-line(centralization-risk)
    function unpause() external onlyOwner {
        _unpause();
    }

    // aderyn-ignore-next-line(centralization-risk) aderyn-ignore-next-line(empty-block)
    function _authorizeUpgrade(address) internal override onlyOwner {}
```
