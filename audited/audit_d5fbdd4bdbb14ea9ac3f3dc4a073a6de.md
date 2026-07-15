### Title
Reorg-Induced Pool Address Swap Allows Stakers to Stake in Unintended Pools - (File: src/ConfidencePoolFactory.sol)

### Summary
The deterministic clone salt in `createPool` encodes only the `agreement` address and the per-agreement pool count. A chain reorg that reorders two pool-creation transactions for the same agreement swaps their CREATE2 addresses, causing a staker who approved tokens for the original address to stake in a different pool with different parameters (scope, expiry, recoveryAddress).

### Finding Description
In `ConfidencePoolFactory.createPool()`, the salt for deterministic cloning is constructed as:

```solidity
bytes32 salt = keccak256(abi.encode(agreement, _poolsByAgreement[agreement].length));
pool = Clones.cloneDeterministic(poolImplementation, salt);
``` [1](#0-0) 

The salt encodes only the `agreement` address and the current pool count for that agreement (the index). When an agreement owner creates two pools for the same agreement in close succession, their addresses are determined solely by creation order: Pool A gets `clone(impl, keccak256(agreement, 0))` and Pool B gets `clone(impl, keccak256(agreement, 1))`.

A chain reorg that reorders these two creation transactions swaps their indices and therefore their CREATE2 addresses. Pool B now occupies the address a staker observed for Pool A, and vice versa.

The protocol explicitly allows multiple pools per agreement: [2](#0-1) 

A staker who:
1. Observes Pool A created at `addr_A` with specific parameters (`accounts`, `expiry`, `recoveryAddress`, `minStake`)
2. Approves the stake token for `addr_A`
3. Submits a `stake()` call to `addr_A`

...may find, after a reorg, that `addr_A` now belongs to Pool B. The `stake()` function performs no verification that the caller is interacting with their intended pool: [3](#0-2) 

If Pool B accepts the same stake token and the amount satisfies Pool B's `minStake`, the stake succeeds silently in the wrong pool.

### Impact Explanation
The staker ends up staked in a pool with materially different parameters than intended:

- **Different `accounts` (scope):** The staker's capital now insures different BattleChain contracts than they evaluated. A CORRUPTED outcome on Pool B's scope triggers a full sweep even if Pool A's scope would have survived.
- **Different `recoveryAddress`:** Under bad-faith CORRUPTED, the entire pool (including stakers' principal) sweeps to `recoveryAddress`. If Pool B has a different `recoveryAddress`, the staker's principal goes to an unintended destination.
- **Different `expiry`:** The staker's commitment period and k=2 bonus accrual window differ from what they agreed to.
- **Permanent lock:** Once `riskWindowStart != 0` is set (first observation of `UNDER_ATTACK`), `withdraw()` is permanently disabled. [4](#0-3) 

The staker cannot recover their funds from Pool B through any permissionless path until resolution.

### Likelihood Explanation
Requires three concurrent conditions: (1) a chain reorg on BattleChain (an EVM-compatible L2 — reorgs are possible though less frequent than L1); (2) an agreement owner creating two pools for the same agreement in close succession within the reorg window; (3) a staker submitting a `stake()` call in the same window after observing the pre-reorg pool address. The protocol explicitly supports multiple pools per agreement, making condition (2) a normal operational pattern. Likelihood is low but non-zero.

### Recommendation
Include pool-specific parameters in the salt to make each pool's address unique to its configuration, not just its creation order:

```solidity
bytes32 salt = keccak256(abi.encode(
    agreement,
    _poolsByAgreement[agreement].length,
    stakeToken,
    expiry,
    msg.sender,
    block.timestamp
));
```

This ensures that even if a reorg reorders two pool-creation transactions, the resulting addresses differ from what any staker observed pre-reorg, making the mismatch detectable rather than silent.

### Proof of Concept
1. Agreement owner creates Pool A for agreement X: `expiry=T1`, `accounts=[A1]`, `recoveryAddress=R1` → deployed at `addr_A = clone(impl, keccak256(X, 0))`
2. Agreement owner creates Pool B for agreement X: `expiry=T2`, `accounts=[A2]`, `recoveryAddress=R2` → deployed at `addr_B = clone(impl, keccak256(X, 1))`
3. Staker observes Pool A at `addr_A`, approves stake token for `addr_A`, submits `stake(amount)` to `addr_A`
4. Chain reorg reorders Pool B's creation before Pool A's:
   - Pool B now has index 0 → `addr_A = clone(impl, keccak256(X, 0))`
   - Pool A now has index 1 → `addr_B = clone(impl, keccak256(X, 1))`
5. Staker's `stake()` executes on `addr_A`, which is now Pool B (different scope `[A2]`, different `recoveryAddress=R2`, different `expiry=T2`)
6. Staker is now staked in Pool B with parameters they never evaluated
7. If the registry later reaches `UNDER_ATTACK`, `withdraw()` is permanently disabled and the staker's principal is locked in Pool B, subject to Pool B's `recoveryAddress=R2` on a CORRUPTED outcome [5](#0-4)

### Citations

**File:** src/ConfidencePoolFactory.sol (L33-35)
```text
    /// many pools — each commits to its own (locked) scope, so duplicates are allowed and
    /// curated off-chain.
    mapping(address agreement => address[] pools) internal _poolsByAgreement;
```

**File:** src/ConfidencePoolFactory.sol (L84-103)
```text
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
```

**File:** src/ConfidencePool.sol (L222-228)
```text
    function stake(uint256 amount) external nonReentrant whenPoolNotPaused {
        if (amount == 0) revert InvalidAmount();
        if (amount < minStake) revert BelowMinStake();
        if (outcome != PoolStates.Outcome.UNRESOLVED) revert OutcomeAlreadySet();
        if (block.timestamp >= expiry) revert StakingClosed();
        _assertDepositsAllowed(_observePoolState());

```

**File:** src/ConfidencePool.sol (L292-300)
```text
        // gate on it so an upstream registry rewind cannot re-open withdrawals.
        if (
            riskWindowStart != 0
                || (state != IAttackRegistry.ContractState.NOT_DEPLOYED
                    && state != IAttackRegistry.ContractState.NEW_DEPLOYMENT
                    && state != IAttackRegistry.ContractState.ATTACK_REQUESTED)
        ) {
            revert WithdrawsDisabled();
        }
```
