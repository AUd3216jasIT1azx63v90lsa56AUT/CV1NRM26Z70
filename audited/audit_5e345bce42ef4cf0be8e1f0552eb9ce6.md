### Title
Flash Loan with Fee-on-Transfer Token Drains Protocol Balance Without Reimbursement Check - (File: src/Midnight.sol)

### Summary

The `flashLoan` function transfers `assets[i]` out to the callback on line 743 and pulls exactly `assets[i]` back on line 750, with no before/after balance check. When a fee-on-transfer token is used, the protocol receives `assets[i] - fee_in` on the return leg, permanently losing `fee_in` tokens per call. No existing check in the function detects or prevents this deficit.

### Finding Description

**Exact code path:**

`src/Midnight.sol` lines 737–751:

```solidity
for (uint256 i = 0; i < tokens.length; i++) {
    SafeTransferLib.safeTransfer(tokens[i], callback, assets[i]);   // L743
}
// ... callback invoked ...
for (uint256 i = 0; i < tokens.length; i++) {
    SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i]); // L750
}
```

`SafeTransferLib.safeTransfer` and `safeTransferFrom` (lines 12–34 of `src/libraries/SafeTransferLib.sol`) only verify that the low-level call succeeded and that the return value, if any, decodes to `true`. They do **not** measure the actual token amount credited to the recipient.

**Root cause:** No balance snapshot is taken before the outbound transfer, and no balance assertion is made after the inbound `transferFrom`. The function trusts that `assets[i]` tokens were returned because the `transferFrom` call did not revert, which is true even for fee-on-transfer tokens — the call succeeds, but the protocol receives `assets[i] - fee_in`.

**Attacker inputs and flow:**

1. A market exists (or is created by any market creator) whose loan or collateral token is a fee-on-transfer ERC20 (e.g., a token with a 1% transfer fee). The protocol holds a balance `B` of this token.
2. Attacker deploys a callback contract that holds at least `fee_out` extra tokens and has approved `address(midnight)` for `assets[i]`.
3. Attacker calls `midnight.flashLoan([token], [assets[i]], callback, data)`.
4. Protocol executes `safeTransfer(token, callback, assets[i])` → protocol balance: `B - assets[i]`; callback receives `assets[i] - fee_out`.
5. Callback's `onFlashLoan` returns `CALLBACK_SUCCESS`.
6. Protocol executes `safeTransferFrom(token, callback, address(this), assets[i])` → callback balance decreases by `assets[i]`; protocol receives `assets[i] - fee_in`.
7. Protocol balance after: `B - fee_in`. Invariant violated.

**Why existing checks fail:**

- `SafeTransferLib` checks only call success and boolean return, not credited amount.
- `flashLoan` has no `balanceBefore` / `balanceAfter` guard.
- The Certora formal verification explicitly assumes away this class of token: `certora/specs/Solvency.spec` line 31 states *"Assume ERC20 tokens transfer correctly: no fee taking from sender or receiver, no rebasing…"* — meaning the formal proofs provide no coverage here.
- `test/FlashloanTest.sol` only tests with standard ERC20 tokens; no fee-on-transfer variant is tested.

### Impact Explanation

The protocol's token balance decreases by `fee_in` per flash loan call. Repeated calls drain the protocol's holdings of any fee-on-transfer token it holds (as loan token reserves, claimable settlement fees, or collateral). This directly violates the core invariant that contract balances must cover collateral, credit redemption, fees, and withdrawable assets.

### Likelihood Explanation

**Preconditions:**
- A fee-on-transfer token must be held by the protocol (requires a market to exist with such a token and liquidity deposited).
- The attacker's callback must hold at least `fee_out` tokens and have approved the protocol.

**Feasibility:** `flashLoan` is fully permissionless — no role, authorization, or gate check. Any address can call it with any token. The attacker does not profit (they also lose `fee_out`), making this a griefing/drain attack rather than a profitable one. Repeatability is unbounded; each call drains `fee_in` more tokens.

### Recommendation

Add a balance check around the flash loan loop:

```solidity
uint256[] memory balancesBefore = new uint256[](tokens.length);
for (uint256 i = 0; i < tokens.length; i++) {
    balancesBefore[i] = IERC20(tokens[i]).balanceOf(address(this));
    SafeTransferLib.safeTransfer(tokens[i], callback, assets[i]);
}
// ... callback ...
for (uint256 i = 0; i < tokens.length; i++) {
    SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i]);
    require(
        IERC20(tokens[i]).balanceOf(address(this)) >= balancesBefore[i],
        InsufficientFlashLoanRepayment()
    );
}
```

This ensures the protocol's balance is at least restored regardless of token transfer mechanics.

### Proof of Concept

**Foundry fuzz test plan:**

```solidity
// FeeOnTransferToken: transfer/transferFrom deduct `feeBps` from credited amount.
// Setup: deal protocol `assets` of FeeOnTransferToken.
// Callback: holds `fee_out` extra tokens, approves midnight for `assets`, returns CALLBACK_SUCCESS.
// Fuzz: feeBps in [1, 1000], assets in [1e6, 1e30].

function testFuzz_flashLoanFeeOnTransfer(uint256 assets, uint256 feeBps) public {
    feeBps = bound(feeBps, 1, 1000);
    assets = bound(assets, 1e6, 1e30);

    uint256 balanceBefore = feeToken.balanceOf(address(midnight));
    vm.prank(attacker);
    midnight.flashLoan([address(feeToken)], [assets], address(callback), "");
    uint256 balanceAfter = feeToken.balanceOf(address(midnight));

    // Assert: protocol lost fee_in tokens
    uint256 feeIn = assets * feeBps / 10000;
    assertEq(balanceBefore - balanceAfter, feeIn, "protocol drained by fee_in");
    // Invariant: balanceAfter >= balanceBefore must hold — this assertion FAILS
    assertGe(balanceAfter, balanceBefore, "balance invariant violated");
}
```

Expected result: the invariant assertion fails; `balanceAfter < balanceBefore` by exactly `fee_in = assets * feeBps / 10_000`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** src/Midnight.sol (L737-751)
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
```

**File:** src/libraries/SafeTransferLib.sol (L12-34)
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
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
        }
        require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferFromReturnedFalse());
    }
```

**File:** certora/specs/Solvency.spec (L31-33)
```text
    // Assume ERC20 tokens transfer correctly: no fee taking from sender or receiver, no rebasing, no blacklisting, no transfer limits.
    function _.transfer(address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, e.msg.sender, a, v) expect(bool);
    function _.transferFrom(address src, address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, src, a, v) expect(bool);
```

**File:** test/FlashloanTest.sol (L34-48)
```text
        for (uint256 i = 0; i < tokens.length; i++) {
            deal(tokens[i], address(midnight), amounts[i]);
        }

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
```
