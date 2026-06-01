### Title
Fee-on-Transfer Loan Token Causes Callback `onRepay` to Receive Inflated `units` While Midnight Receives Fewer Tokens - (File: src/Midnight.sol)

### Summary
In `repay()`, state changes and the `onRepay` callback are executed before `safeTransferFrom`, and `SafeTransferLib.safeTransferFrom` performs no balance-before/after check. When the loan token is fee-on-transfer, `onRepay` receives `units` as the repaid amount but Midnight's balance only increases by `units * (1 - fee_rate)`, breaking both the callback's accounting invariant and the protocol's `withdrawable` solvency invariant.

### Finding Description
The exact code path in `src/Midnight.sol` lines 502–521:

```
502: function repay(Market memory market, uint256 units, address onBehalf, address callback, bytes calldata data) external {
505:     require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
506:     bytes32 id = touchMarket(market);
508:     position[id][onBehalf].debt -= UtilsLib.toUint128(units);       // (1) debt decremented by units
509:     marketState[id].withdrawable += UtilsLib.toUint128(units);      // (2) withdrawable incremented by units
511:     address payer = callback != address(0) ? callback : msg.sender;
514:     if (callback != address(0)) {
516:         IRepayCallback(callback).onRepay(id, market, units, ...);   // (3) callback sees units
519:     }
520:     SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units); // (4) transfer
521: }
```

`SafeTransferLib.safeTransferFrom` (lines 24–34 of `src/libraries/SafeTransferLib.sol`) only checks that the call succeeded and returned `true`. It does **not** compare `balanceOf(address(this))` before and after. With a fee-on-transfer loan token, step (4) transfers `units` from `payer` but Midnight receives only `units * (1 - fee_rate)`.

**Attacker-controlled inputs:**
- Market creator sets `market.loanToken` to a fee-on-transfer ERC20 (market creator is an allowed unprivileged role per the rules).
- Borrower calls `repay(market, units, onBehalf, callbackAddress, data)` with any non-zero `callback`.

**Exploit flow:**
1. Market is created with a fee-on-transfer loan token.
2. Borrower has debt `D` and calls `repay(market, units, onBehalf, callback, data)`.
3. `position[id][onBehalf].debt` is decremented by `units` and `marketState[id].withdrawable` is incremented by `units` — both using the full nominal amount.
4. `onRepay(id, market, units, onBehalf, data)` is called; the callback sees `units` as the repaid amount and may update its own accounting accordingly.
5. `safeTransferFrom` executes; Midnight receives only `units * (1 - fee_rate)` tokens. No revert occurs because `SafeTransferLib` only checks the boolean return value.

**Why existing checks fail:**
- `SafeTransferLib.safeTransferFrom` has no balance-delta check.
- There is no pre/post balance assertion anywhere in `repay()`.
- The callback is invoked before the transfer, so it cannot observe the actual received amount.
- No market creation guard rejects fee-on-transfer tokens.

### Impact Explanation
Two concrete invariants are violated simultaneously:

1. **Callback accounting desync (scoped impact):** `onRepay` receives `units` as the repaid amount. Any integrating protocol that uses this value to track debt repayment, update credit lines, or release collateral will overcount by `units * fee_rate`. This is not a theoretical concern — the `IRepayCallback` interface explicitly passes `units` for this purpose.

2. **Protocol solvency invariant:** `marketState[id].withdrawable` is incremented by `units` but only `units * (1 - fee_rate)` tokens arrive. Lenders calling `withdraw()` will drain the contract faster than tokens arrive, eventually causing `safeTransfer` to revert for later withdrawers (or silently underpay if the contract holds other balances).

### Likelihood Explanation
**Preconditions:**
- A market must use a fee-on-transfer token as `loanToken`. This is permissionless — any address can call `touchMarket` / create a market.
- A borrower must have an active debt position in that market.
- The borrower (or an authorized address) calls `repay()` with a non-zero `callback`.

All three preconditions are reachable by unprivileged actors. The attack is repeatable on every `repay()` call in such a market. Fee-on-transfer tokens (e.g., tokens with a built-in transfer tax) are a well-known token class and are not excluded by any market creation guard in the protocol.

### Recommendation
Add a balance-before/after check in `repay()` to enforce that the actual received amount equals `units`:

```solidity
uint256 balanceBefore = IERC20(market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
require(IERC20(market.loanToken).balanceOf(address(this)) - balanceBefore == units, FeeOnTransferNotSupported());
```

Alternatively, document and enforce at market creation that fee-on-transfer tokens are not supported (e.g., a whitelist or a creation-time balance check).

### Proof of Concept
```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {Market} from "src/interfaces/IMidnight.sol";
import {IRepayCallback} from "src/interfaces/ICallbacks.sol";
import {CALLBACK_SUCCESS} from "src/libraries/ConstantsLib.sol";

// Fee-on-transfer token: deducts 10% on every transferFrom
contract FeeOnTransferToken {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    uint256 constant FEE_BPS = 1000; // 10%

    function mint(address to, uint256 amount) external { balanceOf[to] += amount; }
    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount; return true;
    }
    function transfer(address to, uint256 amount) external returns (bool) {
        balanceOf[msg.sender] -= amount; balanceOf[to] += amount; return true;
    }
    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        allowance[from][msg.sender] -= amount;
        uint256 fee = amount * FEE_BPS / 10000;
        uint256 received = amount - fee;
        balanceOf[from] -= amount;
        balanceOf[to] += received; // only received arrives
        return true;
    }
}

contract RepayCallbackChecker is IRepayCallback {
    uint256 public reportedUnits;
    address midnight;
    FeeOnTransferToken token;

    constructor(address _midnight, FeeOnTransferToken _token) {
        midnight = _midnight; token = _token;
    }

    function onRepay(bytes32, Market memory market, uint256 units, address, bytes memory)
        external returns (bytes32)
    {
        reportedUnits = units;
        token.approve(midnight, units);
        return CALLBACK_SUCCESS;
    }
}

contract FeeOnTransferRepayTest is Test {
    Midnight midnight;
    FeeOnTransferToken loanToken;
    RepayCallbackChecker checker;
    address borrower = address(0xB0);

    function setUp() public {
        midnight = new Midnight(address(this), address(this), address(this), address(this));
        loanToken = new FeeOnTransferToken();
        checker = new RepayCallbackChecker(address(midnight), loanToken);
        // ... setup market, supply collateral, borrow units via take() ...
    }

    function testFeeOnTransferRepayDesync() public {
        uint256 units = 1000e18;
        // Fund callback contract with full units
        loanToken.mint(address(checker), units);

        uint256 balBefore = loanToken.balanceOf(address(midnight));

        vm.prank(borrower);
        midnight.repay(market, units, borrower, address(checker), "");

        uint256 balAfter = loanToken.balanceOf(address(midnight));
        uint256 actualReceived = balAfter - balBefore;

        // ASSERTION 1: callback was told units (1000e18) but midnight only got 900e18
        assertEq(checker.reportedUnits(), units);
        assertLt(actualReceived, units);
        assertEq(actualReceived, units * 9000 / 10000); // 900e18

        // ASSERTION 2: withdrawable is inflated vs actual balance
        assertEq(midnight.withdrawable(id), units);          // 1000e18 in state
        assertEq(loanToken.balanceOf(address(midnight)), actualReceived); // 900e18 in reality
    }
}
```

**Expected assertions:**
- `checker.reportedUnits() == 1000e18` (callback sees full units)
- `loanToken.balanceOf(address(midnight)) == 900e18` (only 90% arrives)
- `midnight.withdrawable(id) == 1000e18` (state is inflated by 100e18)
- Lender calling `withdraw(market, 1000e18, ...)` will drain 100e18 more than Midnight holds, eventually causing revert for subsequent withdrawers [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** src/interfaces/ICallbacks.sol (L20-22)
```text
interface IRepayCallback {
    function onRepay(bytes32 id, Market memory market, uint256 units, address onBehalf, bytes memory data) external returns (bytes32);
}
```
