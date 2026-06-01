### Title
Fee-on-Transfer Loan Token Causes `withdrawable` Overcounting, Enabling Lender Fund Freeze or Cross-User Drain - (File: src/Midnight.sol)

### Summary

`repay()` unconditionally increments `marketState[id].withdrawable` by the caller-supplied `units` parameter before calling `SafeTransferLib.safeTransferFrom`, which does not verify the actual balance delta received. When the market's `loanToken` is a fee-on-transfer token, only `(units - fee)` tokens arrive at the contract while `withdrawable` is inflated by the full `units`. A subsequent `withdraw(units)` by a lender then attempts to `safeTransfer(units)` out, but the contract holds only `(units - fee)`, causing either a revert (fund freeze) or a drain of tokens belonging to other users.

### Finding Description

**Code path — `repay()`** (`src/Midnight.sol` lines 502–521):

```
position[id][onBehalf].debt -= units;
marketState[id].withdrawable += units;          // (1) accounting: +units
...
SafeTransferLib.safeTransferFrom(               // (2) actual receipt: +(units-fee)
    market.loanToken, payer, address(this), units
);
``` [1](#0-0) 

**Code path — `withdraw()`** (`src/Midnight.sol` lines 481–500):

```
_marketState.withdrawable -= units;             // (3) accounting: -units
...
SafeTransferLib.safeTransfer(                   // (4) outbound: tries to send units
    market.loanToken, receiver, units
);
``` [2](#0-1) 

**`SafeTransferLib.safeTransferFrom`** only checks the boolean return value; it performs no balance-before/balance-after comparison and cannot detect that fewer tokens than `units` were actually deposited. [3](#0-2) 

**Market creation is permissionless** — any address can create a market with an arbitrary `loanToken`. The `touchMarket` path imposes no whitelist or token-type validation, so a market creator (an explicitly listed attacker role) can deploy a market whose `loanToken` charges a transfer fee.

**Exploit flow:**

1. Market creator deploys a market whose `loanToken` is a fee-on-transfer ERC20 (e.g., 1 % fee).
2. Lender supplies credit; borrower takes (borrows) `D` units of debt.
3. Borrower calls `repay(market, D, borrower, address(0), "")`.
   - `withdrawable` increases by `D`.
   - `safeTransferFrom` moves only `D * 0.99` tokens into the contract.
   - Gap: `withdrawable` overstates actual balance by `D * 0.01`.
4. Lender calls `withdraw(market, D, lender, lender)`.
   - `withdrawable` decreases by `D` (passes the underflow check because it was inflated).
   - `safeTransfer(loanToken, lender, D)` is attempted.
   - Contract holds only `D * 0.99` → transfer reverts → **lender fund freeze**.
   - If other users' repayments have deposited additional tokens, the transfer succeeds by consuming those tokens → **cross-user drain / theft**.

**Existing protections are insufficient:**

The Certora `Solvency.spec` `tokenBalanceCorrect` invariant explicitly assumes "no fee taking from sender or receiver, no rebasing" — the formal proof does not cover fee-on-transfer tokens. [4](#0-3) 

The `WithdrawableMonotonicity.spec` rule `repayIncreasesWithdrawable` asserts `withdrawableAfter == withdrawableBefore + units`, which is exactly the broken invariant when a fee-on-transfer token is used. [5](#0-4) 

### Impact Explanation

A lender whose credit is backed by repayments made with a fee-on-transfer loan token cannot withdraw their entitled funds: `withdraw()` either reverts (permanent fund freeze until the shortfall is covered by unrelated deposits) or succeeds by consuming tokens that belong to other lenders or fee claimers, constituting direct theft. The core invariant `tokenBalance >= collateralSum + withdrawableSum + claimableSettlementFee` is violated.

### Likelihood Explanation

- Markets are permissionlessly created; any address can be a market creator (explicitly listed attacker role).
- Fee-on-transfer ERC20s are a well-known, deployed token class (e.g., USDT with fee enabled, STA, PAXG with fee).
- No admin action is required after market creation; any borrower can trigger the repay step.
- The shortfall accumulates with every `repay` call, making the impact repeatable and compounding.

### Recommendation

In `repay()` (and symmetrically in `liquidate()`), measure the actual received amount using a balance-before/after pattern and use that delta — not the caller-supplied `units` — for both the debt reduction and the `withdrawable` increment:

```solidity
uint256 balanceBefore = IERC20(market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
uint256 received = IERC20(market.loanToken).balanceOf(address(this)) - balanceBefore;
require(received == units, FeeOnTransferNotSupported());
```

Alternatively, add an explicit token whitelist or a market-creation check that rejects tokens whose `transferFrom` delivers fewer tokens than requested.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {Market} from "src/interfaces/IMidnight.sol";

// Minimal fee-on-transfer ERC20: charges 1% on every transferFrom.
contract FeeToken is ERC20 {
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount / 100;
        super.transferFrom(from, to, amount - fee); // only (amount-fee) arrives
        _burn(from, fee);                           // fee destroyed
        return true;
    }
}

contract FeeOnTransferRepayTest is Test {
    Midnight midnight;
    FeeToken token;
    Market market;
    address lender = address(0xA);
    address borrower = address(0xB);

    function setUp() public {
        midnight = new Midnight(...);
        token = new FeeToken();
        // Create market with FeeToken as loanToken
        // Lender supplies credit D; borrower takes D units of debt
    }

    function testFeeOnTransferRepayWithdrawFreeze() public {
        uint256 D = 1000e18;

        // Borrower repays D units; only 990e18 arrives
        vm.prank(borrower);
        token.approve(address(midnight), D);
        vm.prank(borrower);
        midnight.repay(market, D, borrower, address(0), "");

        // Assert: withdrawable == D (inflated)
        assertEq(midnight.withdrawable(marketId), D);
        // Assert: actual contract balance == 990e18 (shortfall)
        assertEq(token.balanceOf(address(midnight)), 990e18);

        // Lender attempts to withdraw D units
        vm.prank(lender);
        vm.expectRevert(); // transfer fails: contract has 990e18, needs 1000e18
        midnight.withdraw(market, D, lender, lender);

        // OR: if other tokens present, assert lender receives D but
        // another user's balance decreases by 10e18 (theft).
    }
}
```

**Expected assertions:**
- `withdrawable == D` while `token.balanceOf(midnight) == D - fee` → invariant broken.
- `withdraw(D)` reverts (fund freeze), confirming the scoped impact.
- With a second depositor present, `withdraw(D)` succeeds but the second depositor's withdrawable is unrecoverable (theft).

### Citations

**File:** src/Midnight.sol (L494-499)
```text
        _marketState.withdrawable -= UtilsLib.toUint128(units);
        _marketState.totalUnits -= UtilsLib.toUint128(units);

        emit EventsLib.Withdraw(msg.sender, id, units, onBehalf, receiver, pendingFeeDecrease);

        SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
```

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

**File:** certora/specs/Solvency.spec (L31-33)
```text
    // Assume ERC20 tokens transfer correctly: no fee taking from sender or receiver, no rebasing, no blacklisting, no transfer limits.
    function _.transfer(address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, e.msg.sender, a, v) expect(bool);
    function _.transferFrom(address src, address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, src, a, v) expect(bool);
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
