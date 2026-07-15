### Title
Unprotected `initialize()` Allows Front-Running to Seize Factory Ownership and Inject Malicious Pool Implementation - (File: src/ConfidencePoolFactory.sol)

### Summary
`ConfidencePoolFactory.initialize()` is `external` with no access control beyond the `initializer` modifier. If the proxy is deployed without atomic initialization (i.e., proxy deployment and `initialize()` call are separate transactions), any attacker can front-run the legitimate initialization, become the factory owner, and inject a malicious `poolImplementation` that drains all future staker funds.

### Finding Description
`ConfidencePoolFactory.initialize()` is declared `external` and guarded only by OpenZeppelin's `initializer` modifier:

```solidity
function initialize(address safeHarborRegistry_, address poolImplementation_, address defaultOutcomeModerator_)
    external
    initializer
{
    ...
    __Ownable_init(msg.sender);   // owner = whoever calls first
    ...
    poolImplementation = poolImplementation_;
    defaultOutcomeModerator = defaultOutcomeModerator_;
}
```

The `initializer` modifier prevents *re-initialization* but imposes no restriction on *who* may perform the first initialization. Ownership is unconditionally assigned to `msg.sender`. An attacker who observes the proxy deployment transaction in the mempool (or who deploys a proxy themselves against the known implementation address) can call `initialize()` before the legitimate deployer, supplying:

- themselves as the implicit owner (via `msg.sender`)
- a malicious contract as `poolImplementation_`
- a malicious address as `defaultOutcomeModerator_`

The constructor of the implementation correctly calls `_disableInitializers()`, protecting the bare implementation, but the proxy's storage is independent and starts with `_initialized == 0`, leaving the proxy's initialization window open.

### Impact Explanation
A successful front-run grants the attacker full factory ownership. Concrete consequences:

1. **Malicious pool implementation.** `createPool()` clones `poolImplementation` via `Clones.cloneDeterministic`. A substituted implementation can be written to transfer all deposited stake and bonus tokens to the attacker on any call, draining every pool created through the factory.
2. **Malicious moderator.** `defaultOutcomeModerator` is passed verbatim to every new pool's `initialize()`. An attacker-controlled moderator can call `flagOutcome(CORRUPTED, false, ...)` on any pool, routing the full pool balance to `recoveryAddress` (also sponsor-controlled), or `flagOutcome(CORRUPTED, true, attacker)` to redirect funds to themselves via `claimAttackerBounty`.
3. **Allowlist control.** The attacker can whitelist malicious ERC20 tokens via `setStakeTokenAllowed`, enabling fee-on-transfer or rebasing tokens that silently under-pay claims.
4. **UUPS upgrade authority.** `_authorizeUpgrade` is `onlyOwner`; the attacker can upgrade the factory proxy to arbitrary logic.
5. **Legitimate deployer locked out.** The `initializer` modifier ensures the legitimate `initialize()` call reverts `InvalidInitialization`, leaving the attacker in permanent control.

### Likelihood Explanation
Likelihood is medium. The attack requires either (a) the deploy script submitting proxy deployment and `initialize()` as separate transactions (observable in the mempool), or (b) a predictable proxy address (deterministic deployment) that an attacker can target before the deployer acts. UUPS proxy deployments that pass initialization calldata to the `ERC1967Proxy` constructor are atomic and not vulnerable; deployments that call `initialize()` in a follow-up transaction are. The deploy script (`script/Deploy.s.sol`) exists but its atomicity is unverified from available sources. The vulnerability class is present in the contract regardless of deployment tooling.

### Recommendation
Pass the initialization calldata directly to the `ERC1967Proxy` constructor so deployment and initialization are atomic within a single transaction:

```solidity
new ERC1967Proxy(
    address(impl),
    abi.encodeCall(ConfidencePoolFactory.initialize, (registry, poolImpl, moderator))
);
```

Alternatively, add a deployer-address check inside `initialize()` (e.g., an immutable set in the constructor) so only the intended deployer can call it, mirroring the pattern used for `ConfidencePool` clones where `createPool()` calls `initialize()` atomically in the same transaction.

### Proof of Concept
1. Attacker monitors the mempool and observes the `ERC1967Proxy` deployment transaction targeting the `ConfidencePoolFactory` implementation, with empty initialization data.
2. Before the deployer's follow-up `initialize()` transaction is mined, attacker submits a higher-gas transaction calling:
   ```solidity
   ConfidencePoolFactory(proxyAddress).initialize(
       legitimateRegistry,
       address(maliciousPoolImpl),  // attacker-controlled implementation
       address(attackerModerator)   // attacker-controlled moderator
   );
   ```
3. Attacker's transaction is mined first. `__Ownable_init(msg.sender)` sets attacker as owner. `poolImplementation` is set to `maliciousPoolImpl`.
4. Legitimate deployer's `initialize()` reverts with `InvalidInitialization`.
5. Any sponsor calling `createPool()` receives a clone of `maliciousPoolImpl`. When stakers call `stake()`, the malicious implementation transfers tokens to the attacker instead of recording them.
6. Attacker additionally calls `setStakeTokenAllowed` and `setDefaultOutcomeModerator` to further entrench control, and calls `upgradeToAndCall` to replace the factory proxy logic entirely. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** src/ConfidencePoolFactory.sol (L86-101)
```text
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

**File:** src/ConfidencePoolFactory.sol (L173-174)
```text
    // aderyn-ignore-next-line(centralization-risk) aderyn-ignore-next-line(empty-block)
    function _authorizeUpgrade(address) internal override onlyOwner {}
```
