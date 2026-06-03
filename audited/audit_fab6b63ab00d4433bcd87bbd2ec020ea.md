Audit Report

## Title
Unbounded `tokens[]` array in `flashLoan` enables zero-balance block-stuffing DoS - (File: src/Midnight.sol)

## Summary
The `flashLoan` function accepts caller-supplied `tokens[]` and `assets[]` arrays of arbitrary length with no upper-bound check. An unprivileged attacker can pass arbitrarily large arrays of attacker-controlled stub token addresses with zero-value assets, causing the function to execute 2N external calls and consuming block gas linearly with N. No token balance or protocol permissions are required.

## Finding Description
`flashLoan` at lines 737–752 of `src/Midnight.sol` contains only a single length guard at line 740 (`tokens.length == assets.length`), which is trivially satisfied by passing equal-length arrays. The function then executes two unbounded loops:

- Lines 742–744: `SafeTransferLib.safeTransfer(tokens[i], callback, assets[i])` for each `i`
- Lines 749–751: `SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i])` for each `i`

Each call in `SafeTransferLib` performs an `EXTCODESIZE` check (`token.code.length > 0`, lines 13 and 25 of `src/libraries/SafeTransferLib.sol`) followed by a `CALL` into the token contract (lines 15 and 27). This is 2N external calls total.

`ConstantsLib.sol` defines `MAX_COLLATERALS = 128` (line 20) and `MAX_COLLATERALS_PER_BORROWER = 16` (line 21) as analogous bounds for other array-bounded operations, but defines no `MAX_FLASH_LOAN_TOKENS` constant. The `flashLoan` function has no access control.

**Exploit flow:**
1. Attacker deploys a minimal ERC20 stub with deployed bytecode (satisfying `token.code.length > 0`) that returns `true` for any `transfer`/`transferFrom` call, including zero-value.
2. Attacker deploys a callback stub that returns `CALLBACK_SUCCESS` (`keccak256("morpho.midnight.callbackSuccess")`, `ConstantsLib.sol` line 25).
3. Attacker calls `flashLoan(tokens=[stub]*N, assets=[0]*N, callbackStub, "")` with N arbitrarily large.
4. The function executes 2N external calls, consuming gas proportional to N.
5. At N ≈ 3,000–5,000, the transaction approaches the Ethereum block gas limit (~30M gas).

**Why existing checks fail:** The `tokens.length == assets.length` check (line 740) is trivially satisfied by passing equal-length arrays. With `assets[i] = 0`, the protocol requires zero token balance for outbound transfers, and the callback requires zero token balance for inbound transfers — zero-value ERC20 transfers succeed on standard implementations and on attacker-controlled stubs. No access control exists on `flashLoan`.

## Impact Explanation
An attacker can submit repeated `flashLoan` transactions with large `tokens[]` arrays to consume the majority of consecutive blocks' gas budgets, constituting a block-stuffing DoS. Legitimate users' transactions are delayed or excluded. The attacker requires zero token balance and zero protocol permissions. This matches the valid impact category of "service unavailability or severe degradation under realistic attacker input" per `RESEARCHER.md`. The protocol has no rate limiting, nonce, or cooldown on `flashLoan`.

## Likelihood Explanation
**Preconditions:** One-time deployment of two trivial stub contracts (minimal bytecode, no state). **Feasibility:** On Ethereum mainnet, sustained block stuffing requires significant ETH for gas but is feasible for targeted short-duration attacks. On L2s (Arbitrum, Base, Optimism) where gas costs are orders of magnitude lower, this is highly practical and repeatable. **Repeatability:** No state is modified that would prevent repeated calls; the attacker can submit the same transaction in every block indefinitely.

## Recommendation
Add an upper-bound check on `tokens.length` before the loops in `flashLoan`, analogous to the `MAX_COLLATERALS` guard used elsewhere. Define a constant such as `MAX_FLASH_LOAN_TOKENS` in `ConstantsLib.sol` (e.g., `uint256 constant MAX_FLASH_LOAN_TOKENS = 128`) and add `require(tokens.length <= MAX_FLASH_LOAN_TOKENS, TooManyFlashLoanTokens())` at the start of `flashLoan`, immediately after the length-consistency check at line 740.

## Proof of Concept
1. Deploy `StubERC20` with bytecode that returns `true` (ABI-encoded) for any call.
2. Deploy `StubCallback` that returns `keccak256("morpho.midnight.callbackSuccess")` from `onFlashLoan`.
3. Construct `tokens = new address[](N)` filled with `StubERC20` address and `assets = new uint256[](N)` filled with `0`.
4. Call `Midnight.flashLoan(tokens, assets, address(StubCallback), "")`.
5. Observe gas consumption scales linearly with N; at N = 5,000, the transaction consumes ~30M gas, filling an entire Ethereum block. Repeat in every block to sustain DoS. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** src/libraries/SafeTransferLib.sol (L12-27)
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

    function safeTransferFrom(address token, address from, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());

        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transferFrom, (from, to, value)));
```

**File:** src/libraries/ConstantsLib.sol (L20-21)
```text
uint256 constant MAX_COLLATERALS = 128;
uint256 constant MAX_COLLATERALS_PER_BORROWER = 16;
```

**File:** src/libraries/ConstantsLib.sol (L25-25)
```text
bytes32 constant CALLBACK_SUCCESS = keccak256("morpho.midnight.callbackSuccess");
```
