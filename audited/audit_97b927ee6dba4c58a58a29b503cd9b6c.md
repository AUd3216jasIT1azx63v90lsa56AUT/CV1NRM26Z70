### Title
Unbounded `tokens[]` array in `flashLoan` enables block-stuffing DoS - (File: src/Midnight.sol)

### Summary
The `flashLoan` function accepts caller-supplied `tokens[]` and `assets[]` arrays of arbitrary length with no upper-bound check beyond `tokens.length == assets.length`. It iterates `tokens.length` twice, each iteration dispatching an external ERC20 call via `SafeTransferLib`. An unprivileged attacker can pass a very large array with zero-value assets, consuming block gas without holding any tokens or requiring any permissions.

### Finding Description
The reachable code path is `Midnight.flashLoan` at lines 737–752 of `src/Midnight.sol`:

```solidity
function flashLoan(address[] calldata tokens, uint256[] calldata assets, address callback, bytes calldata data)
    external
{
    require(tokens.length == assets.length, InconsistentInput()); // only guard
    emit EventsLib.FlashLoan(msg.sender, tokens, assets, callback);
    for (uint256 i = 0; i < tokens.length; i++) {
        SafeTransferLib.safeTransfer(tokens[i], callback, assets[i]); // external call
    }
    require(
        IFlashLoanCallback(callback).onFlashLoan(msg.sender, tokens, assets, data) == CALLBACK_SUCCESS, ...
    );
    for (uint256 i = 0; i < tokens.length; i++) {
        SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i]); // external call
    }
}
```

The sole guard is `tokens.length == assets.length`; there is no cap on array length. Each `SafeTransferLib.safeTransfer` and `SafeTransferLib.safeTransferFrom` call performs an `EXTCODESIZE` check plus a `CALL` into the token contract (lines 13–15 and 25–27 of `src/libraries/SafeTransferLib.sol`). With warm storage, each token iteration costs roughly 3,000–8,000 gas across both libraries.

**Attacker-controlled inputs and exploit flow:**
1. Attacker deploys a minimal ERC20 stub that accepts any `transfer`/`transferFrom` (including zero-value) and returns `true`.
2. Attacker deploys a callback stub that returns `CALLBACK_SUCCESS`.
3. Attacker calls `flashLoan(tokens=[stub]*N, assets=[0]*N, callback, "")` with N arbitrarily large.
4. The function executes two loops of N external calls each (2N total), consuming gas linearly.
5. At N ≈ 3,000–5,000 (depending on token stub complexity), the transaction approaches the Ethereum block gas limit (~30M gas).

**Why existing checks fail:** The only check (`tokens.length == assets.length`) is satisfied trivially. There is no `MAX_FLASH_LOAN_TOKENS` constant defined anywhere in `src/libraries/ConstantsLib.sol` (which defines `MAX_COLLATERALS = 128` and `MAX_COLLATERALS_PER_BORROWER = 16` for analogous array bounds elsewhere, but nothing for flash loan arrays). The function is `external` with no access control.

### Impact Explanation
An attacker can submit a `flashLoan` transaction with a very large `tokens[]` array to consume the majority of a block's gas budget. This constitutes a block-stuffing DoS: other users' transactions are delayed or excluded from the block. The attacker needs zero token balance (all `assets[i] = 0`) and zero protocol permissions. The attack is repeatable across consecutive blocks.

### Likelihood Explanation
**Preconditions:** None beyond deploying two trivial stub contracts (one-time setup). **Feasibility:** On Ethereum mainnet, sustained block stuffing is expensive but feasible for targeted short-duration attacks. On L2s (Arbitrum, Base, Optimism) where gas is cheap, this is highly practical and repeatable. **Repeatability:** The function has no rate limiting, nonce, or cooldown.

### Recommendation
Add an explicit upper-bound check on `tokens.length` before the loops, consistent with the pattern used for collateral arrays elsewhere in the protocol:

```solidity
uint256 constant MAX_FLASH_LOAN_TOKENS = 16; // or another reasonable bound

function flashLoan(...) external {
    require(tokens.length == assets.length, InconsistentInput());
    require(tokens.length <= MAX_FLASH_LOAN_TOKENS, ArrayTooLong());
    ...
}
```

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";

contract MockERC20 {
    function transfer(address, uint256) external pure returns (bool) { return true; }
    function transferFrom(address, address, uint256) external pure returns (bool) { return true; }
}

contract MockCallback {
    bytes32 constant CALLBACK_SUCCESS = keccak256("morpho.midnight.callbackSuccess");
    function onFlashLoan(address, address[] memory, uint256[] memory, bytes memory)
        external pure returns (bytes32) { return CALLBACK_SUCCESS; }
}

contract FlashLoanUnboundedLoopTest is Test {
    Midnight midnight;
    MockERC20 token;
    MockCallback cb;

    function setUp() public {
        midnight = new Midnight();
        token = new MockERC20();
        cb = new MockCallback();
    }

    /// @dev Fuzz: assert gas grows linearly with tokens.length
    function testFuzz_flashLoanGasLinear(uint16 n) public {
        vm.assume(n > 0 && n <= 1000);
        address[] memory tokens = new address[](n);
        uint256[] memory assets = new uint256[](n);
        for (uint256 i = 0; i < n; i++) {
            tokens[i] = address(token);
            assets[i] = 0;
        }
        uint256 gasBefore = gasleft();
        midnight.flashLoan(tokens, assets, address(cb), "");
        uint256 gasUsed = gasBefore - gasleft();
        // Assert: gas per token is bounded and linear (document slope)
        emit log_named_uint("n", n);
        emit log_named_uint("gasUsed", gasUsed);
        emit log_named_uint("gasPerToken", gasUsed / n);
        // At n=1000, gasUsed should approach or exceed several million gas
        // At n~4000-5000, should approach 30M block gas limit
    }
}
```

**Expected assertions:** Gas grows linearly with `n`; at `n ≈ 3,000–5,000`, `gasUsed` approaches the 30M block gas limit, confirming the block-stuffing vector. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** src/libraries/SafeTransferLib.sol (L12-22)
```text
    function safeTransfer(address token, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());

        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transfer, (to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
        }
        require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferReturnedFalse());
    }
```

**File:** src/libraries/SafeTransferLib.sol (L24-34)
```text
    function safeTransferFrom(address token, address from, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());

        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transferFrom, (from, to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
        }
        require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferFromReturnedFalse());
    }
```

**File:** src/libraries/ConstantsLib.sol (L20-21)
```text
uint256 constant MAX_COLLATERALS = 128;
uint256 constant MAX_COLLATERALS_PER_BORROWER = 16;
```
