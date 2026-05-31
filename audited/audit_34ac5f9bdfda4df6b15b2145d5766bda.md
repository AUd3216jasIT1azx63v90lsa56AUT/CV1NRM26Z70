### Title
`withdraw()` unconditionally calls `safeTransfer` with zero value, reverting on tokens that reject zero-value transfers - (`src/libraries/SafeTransferLib.sol` / `src/Midnight.sol`)

### Summary
`withdraw()` in `src/Midnight.sol` passes `units` directly to `SafeTransferLib.safeTransfer` with no zero-value guard. `SafeTransferLib.safeTransfer` always issues the low-level `transfer(to, value)` call regardless of whether `value` is zero. For any loan token that reverts on zero-value transfers — a realistic and documented token behavior — calling `withdraw(units=0)` will revert, even though the protocol's own fuzz suite explicitly treats `units=0` as a valid input.

### Finding Description
**Code path:**

`withdraw()` (`src/Midnight.sol:481–500`) performs all arithmetic cleanly when `units=0` (the subtractions at lines 493–495 are no-ops, `mulDivUp(0, _position.credit)` returns 0), then unconditionally reaches:

```solidity
SafeTransferLib.safeTransfer(market.loanToken, receiver, units); // line 499
```

`SafeTransferLib.safeTransfer` (`src/libraries/SafeTransferLib.sol:12–22`) has no early-exit for `value == 0`:

```solidity
(bool success, bytes memory returndata) =
    token.call(abi.encodeCall(IERC20.transfer, (to, value))); // line 15 — always called
if (!success) {
    assembly ("memory-safe") { revert(add(returndata, 0x20), mload(returndata)) }
}
```

If the loan token's `transfer(to, 0)` reverts (e.g., tokens that enforce `amount > 0`), the revert propagates out of `withdraw()` with no protocol-level catch.

**Protocol intent for units=0:** The fuzz test `testWithdrawReducesPendingFee` (`test/ContinuousFeeTest.sol:331`) explicitly bounds `withdrawAmount = bound(withdrawAmount, 0, creditAfterAccrual)`, including 0 in the valid domain. No `require(units > 0)` guard exists anywhere in `withdraw()`.

**Attacker-controlled inputs:** The lender (unprivileged) supplies `units = 0`. No special state is required — the call is valid as long as authorization passes (line 482).

**Why existing checks fail:** The only pre-transfer checks are the authorization guard (line 482) and the `if (_position.credit > 0)` branch (line 489), neither of which prevents the zero-value `safeTransfer` call.

### Impact Explanation
A lender calling `withdraw(market, 0, onBehalf, receiver)` on any market whose loan token reverts on zero-value transfers receives an unexpected revert. This blocks a valid no-op operation that the protocol's own test suite treats as correct, violating the invariant that no-ops must not revert. Any integration or keeper that uses `units=0` to trigger position updates via `_updatePosition` without moving funds is also broken.

### Likelihood Explanation
Tokens that revert on zero-value `transfer` calls exist in production (e.g., tokens enforcing `require(amount > 0)`). The precondition is simply: (1) the market's loan token reverts on `transfer(to, 0)`, and (2) a lender calls `withdraw` with `units=0`. No privileged action, oracle manipulation, or user mistake is required. The call is repeatable on every such market.

### Recommendation
Add a zero-value early return at the top of `withdraw()` (and symmetrically in `withdrawCollateral` for consistency):

```solidity
function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external {
    if (units == 0) return; // no-op guard
    ...
    SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
}
```

Alternatively, add the guard inside `SafeTransferLib.safeTransfer` itself:

```solidity
function safeTransfer(address token, address to, uint256 value) internal {
    if (value == 0) return;
    ...
}
```

The function-level guard is preferable because it also avoids the unnecessary `_updatePosition`, storage writes, and event emission for a true no-op.

### Proof of Concept
```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {Market} from "src/interfaces/IMidnight.sol";

/// @dev Loan token that reverts on zero-value transfers.
contract RevertOnZeroERC20 {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function transfer(address to, uint256 amount) external returns (bool) {
        require(amount > 0, "zero transfer");  // realistic guard
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }
    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        require(amount > 0, "zero transfer");
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }
    function mint(address to, uint256 amount) external { balanceOf[to] += amount; }
}

contract WithdrawZeroRevertTest is Test {
    Midnight midnight;
    RevertOnZeroERC20 loanToken;
    address lender = makeAddr("lender");

    function setUp() public {
        loanToken = new RevertOnZeroERC20();
        midnight = new Midnight(address(this));
        // ... configure market with loanToken, set up lender credit position ...
    }

    function testWithdrawZeroRevertsOnZeroTransferToken() public {
        Market memory market = /* market with loanToken */;
        // Lender has credit > 0 from a prior take/repay cycle.
        // Calling withdraw(0) should be a no-op but reverts instead.
        vm.prank(lender);
        vm.expectRevert("zero transfer"); // token's revert propagates
        midnight.withdraw(market, 0, lender, lender);

        // Expected: call succeeds silently (no state change, no transfer).
        // Actual:   reverts with the token's zero-transfer error.
    }
}
```

**Key assertions:**
- `vm.expectRevert("zero transfer")` confirms the revert originates from the token, not a protocol guard.
- After the fix (`if (units == 0) return;`), the same call must succeed without reverting and leave all state unchanged. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** src/Midnight.sol (L481-500)
```text
    function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        MarketState storage _marketState = marketState[id];
        _updatePosition(market, id, onBehalf);

        Position storage _position = position[id][onBehalf];
        uint128 pendingFeeDecrease;
        if (_position.credit > 0) {
            pendingFeeDecrease = UtilsLib.toUint128(_position.pendingFee.mulDivUp(units, _position.credit));
            _position.pendingFee -= pendingFeeDecrease;
        }
        _position.credit -= UtilsLib.toUint128(units);
        _marketState.withdrawable -= UtilsLib.toUint128(units);
        _marketState.totalUnits -= UtilsLib.toUint128(units);

        emit EventsLib.Withdraw(msg.sender, id, units, onBehalf, receiver, pendingFeeDecrease);

        SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
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

**File:** test/ContinuousFeeTest.sol (L331-331)
```text
        withdrawAmount = bound(withdrawAmount, 0, creditAfterAccrual);
```
