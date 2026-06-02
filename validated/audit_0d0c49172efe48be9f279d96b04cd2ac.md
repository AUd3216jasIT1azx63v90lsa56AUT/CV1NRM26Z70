Audit Report

## Title
Unbounded `tokens[]` array in `flashLoan` enables zero-cost block-stuffing DoS - (File: src/Midnight.sol)

## Summary
The `flashLoan` function at lines 737–752 of `src/Midnight.sol` accepts caller-supplied `tokens[]` and `assets[]` arrays of arbitrary length with no upper-bound check. An unprivileged attacker can pass a large array of zero-value assets against stub ERC20 contracts, causing the function to execute 2N external calls and consuming block gas linearly with N. This enables repeatable block-stuffing DoS with zero token balance required.

## Finding Description
The sole input guard in `flashLoan` is `tokens.length == assets.length` at line 740, which is trivially satisfied. The function then executes two loops over `tokens.length` iterations: lines 742–744 call `SafeTransferLib.safeTransfer` for each token, and lines 749–751 call `SafeTransferLib.safeTransferFrom` for each token. Each `safeTransfer` call performs an `EXTCODESIZE` check (`token.code.length > 0`, line 13 of `SafeTransferLib.sol`) followed by a `CALL` into the token contract (line 15). Likewise for `safeTransferFrom` (lines 25–27). This yields 2N external calls total.

`ConstantsLib.sol` defines `MAX_COLLATERALS = 128` and `MAX_COLLATERALS_PER_BORROWER = 16` for analogous array bounds elsewhere, but defines no `MAX_FLASH_LOAN_TOKENS` constant. No access control modifier exists on `flashLoan`.

**Exploit flow:**
1. Attacker deploys a minimal ERC20 stub with deployed bytecode (satisfying `token.code.length > 0`) that returns `true` for any `transfer`/`transferFrom` call including zero-value.
2. Attacker deploys a callback stub that returns `CALLBACK_SUCCESS` (line 746 check).
3. Attacker calls `flashLoan(tokens=[stub]*N, assets=[0]*N, callback, "")` with N arbitrarily large.
4. The function executes 2N external calls, consuming gas linearly with N.
5. At N ≈ 3,000–5,000, the transaction approaches the Ethereum block gas limit (~30M gas).

With `assets[i] = 0`, the protocol requires zero token balance for outbound transfers, and the callback requires zero token balance for inbound transfers. The `tokens.length == assets.length` check is trivially satisfied. No permissions are required.

## Impact Explanation
An attacker can submit repeated `flashLoan` transactions with large `tokens[]` arrays to consume the majority of consecutive blocks' gas budgets. This constitutes a block-stuffing DoS: legitimate users' transactions are delayed or excluded. The attacker requires zero token balance and zero protocol permissions. This matches the valid impact category of "service unavailability or severe degradation under realistic attacker input" per `RESEARCHER.md`, and "resource exhaustion paths" listed as a priority attack surface.

## Likelihood Explanation
**Preconditions:** One-time deployment of two trivial stub contracts. **Feasibility:** On Ethereum mainnet, sustained block stuffing is costly but feasible for targeted short-duration attacks. On L2s (Arbitrum, Base, Optimism) where gas is cheap, this is highly practical and repeatable. **Repeatability:** No rate limiting, nonce, or cooldown exists on `flashLoan`. The function is permissionless and callable by any external address.

## Recommendation
Add an upper-bound check on `tokens.length` in `flashLoan`, analogous to the existing `MAX_COLLATERALS` pattern used elsewhere in the protocol. Define a new constant (e.g., `MAX_FLASH_LOAN_TOKENS`) in `ConstantsLib.sol` and add `require(tokens.length <= MAX_FLASH_LOAN_TOKENS, ...)` at the start of `flashLoan`, before the loops. A reasonable value would be consistent with `MAX_COLLATERALS = 128` or smaller.

## Proof of Concept
1. Deploy `StubERC20` with bytecode that returns `true` for any `transfer(address,uint256)` and `transferFrom(address,address,uint256)` call.
2. Deploy `StubCallback` that implements `onFlashLoan` returning `CALLBACK_SUCCESS`.
3. Construct `tokens = [address(stubERC20)] * N` and `assets = [0] * N` for large N (e.g., 4000).
4. Call `Midnight.flashLoan(tokens, assets, address(stubCallback), "")`.
5. Observe gas consumption approaching the block gas limit; confirm the transaction succeeds (no revert), demonstrating that 2N external calls execute without any token balance or permissions. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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
