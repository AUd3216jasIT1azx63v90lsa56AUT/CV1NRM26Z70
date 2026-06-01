### Title
Fee-on-Transfer Loan Token Causes `withdrawable` Over-Accounting in `repay` - (`src/Midnight.sol`)

### Summary
The `repay` function unconditionally increments `marketState[id].withdrawable` by `units` before calling `safeTransferFrom`, which only verifies the call succeeded and returned `true`. It does not verify the actual balance increase. When the loan token charges a transfer fee `f`, Midnight receives `units*(1-f)` but records `withdrawable += units`, creating a deficit of `f*units` per repay call that accumulates until lender withdrawals revert for insufficient balance.

### Finding Description
**Code path:**

`src/Midnight.sol` lines 508–520: [1](#0-0) 

```
position[id][onBehalf].debt -= UtilsLib.toUint128(units);      // line 508
marketState[id].withdrawable += UtilsLib.toUint128(units);     // line 509
...
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units); // line 520
```

`src/libraries/SafeTransferLib.sol` lines 24–34: [2](#0-1) 

`safeTransferFrom` calls `transferFrom(from, to, value)` and only checks that the call did not revert and returned `true` (or no return value). It performs **no balance-before/balance-after check** and cannot detect that the contract received fewer tokens than `value`.

**Root cause:** Missing received-amount verification in `safeTransferFrom` combined with optimistic accounting in `repay`.

**Exploit flow:**
1. A market is created with a fee-on-transfer ERC-20 as `loanToken` (fee rate `f` bps). Market creation is permissionless.
2. Borrower takes a loan of `units` via `take`.
3. Borrower calls `repay(market, units, onBehalf, address(0), "")`.
4. `position[id][onBehalf].debt -= units` — debt cleared.
5. `marketState[id].withdrawable += units` — full `units` credited.
6. `safeTransferFrom(loanToken, msg.sender, address(this), units)` — token transfers `units` but deducts fee; Midnight receives `units*(1-f)`.
7. `safeTransferFrom` returns without error because `transferFrom` returned `true`.
8. Net result: `withdrawable` overstated by `f*units`; contract balance short by `f*units`.

**Why existing checks fail:** `SafeTransferLib.safeTransferFrom` only validates `success` and the boolean return value. [3](#0-2) 
There is no `balanceOf(address(this))` snapshot comparison. The Certora spec `WithdrawableMonotonicity.spec` asserts `withdrawableAfter == withdrawableBefore + units` as a rule, but this is only verified against the standard token model — it does not account for fee-on-transfer tokens. [4](#0-3) 

### Impact Explanation
Each `repay` call with a fee-on-transfer loan token causes `withdrawable` to exceed the contract's actual token balance by `f*units`. Lenders calling `withdraw` will drain the real balance before all `withdrawable` credit is consumed, causing the final lenders' `withdraw` calls to revert with insufficient balance. The protocol becomes insolvent by the cumulative sum of all fee shortfalls. Debt is fully erased while the backing assets are not fully present.

### Likelihood Explanation
**Preconditions:** The loan token must charge a transfer fee. Market creation is permissionless, so any actor can deploy a market with such a token. The borrower need only have an outstanding debt position and call `repay` normally — no special privilege, no reentrancy, no oracle manipulation. The attack is repeatable on every `repay` call and compounds linearly with volume.

### Recommendation
Replace the optimistic `units` accounting with a balance-delta check. Measure `balanceOf(address(this))` before and after the `safeTransferFrom` call and use the actual received amount for the `withdrawable` increment:

```solidity
uint256 balanceBefore = IERC20(market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
uint256 received = IERC20(market.loanToken).balanceOf(address(this)) - balanceBefore;
require(received == units, InvalidTransferAmount());
```

Alternatively, document that fee-on-transfer tokens are explicitly unsupported and add a market creation guard (e.g., a balance-delta probe during `enableMarket`).

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";

// FeeToken: transferFrom deducts 1% fee, returns true
contract FeeToken is ERC20 {
    constructor() ERC20("Fee", "FEE") { _mint(msg.sender, 1e30); }
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount / 100; // 1%
        super.transferFrom(from, to, amount - fee);
        return true;
    }
}

contract FeeOnTransferRepayTest is Test {
    Midnight midnight;
    FeeToken token;
    Market market;
    bytes32 id;

    function setUp() public { /* deploy midnight, create market with FeeToken as loanToken, lender supplies, borrower takes */ }

    function testFeeOnTransferRepayInsolvency() public {
        uint256 units = 1000e18;
        // borrower has debt of `units`
        uint256 balBefore = token.balanceOf(address(midnight));
        uint256 withdrawableBefore = midnight.withdrawable(id);

        vm.prank(borrower);
        token.approve(address(midnight), units);
        vm.prank(borrower);
        midnight.repay(market, units, borrower, address(0), "");

        uint256 balAfter = token.balanceOf(address(midnight));
        uint256 withdrawableAfter = midnight.withdrawable(id);

        uint256 received = balAfter - balBefore;
        uint256 recorded = withdrawableAfter - withdrawableBefore;

        // Assert: protocol recorded more than it received
        assertLt(received, recorded, "fee-on-transfer: received < recorded");
        // received == units * 0.99, recorded == units
        assertEq(recorded - received, units / 100, "deficit equals fee");
    }
}
```

**Expected assertions:** `received < recorded` passes; `deficit == f*units` passes. Subsequent `withdraw` by lender for full `units` reverts with insufficient balance once the deficit accumulates.

### Citations

**File:** src/Midnight.sol (L508-520)
```text
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

**File:** certora/specs/WithdrawableMonotonicity.spec (L11-17)
```text
rule repayIncreasesWithdrawable(env e, Midnight.Market market, uint256 units, address onBehalf, address callback, bytes data) {
    bytes32 id = toId(e, market);
    uint256 withdrawableBefore = withdrawable(id);
    repay(e, market, units, onBehalf, callback, data);
    uint256 withdrawableAfter = withdrawable(id);
    assert withdrawableAfter == withdrawableBefore + units;
}
```
