Looking at the exact code path in `repay()` and `SafeTransferLib.safeTransferFrom()`:

### Title
Fee-on-Transfer Loan Token Breaks `withdrawable` Accounting in `repay()`, Causing Lender Insolvency - (File: src/Midnight.sol)

### Summary
`repay()` unconditionally increments `marketState[id].withdrawable` by the full `units` parameter before calling `safeTransferFrom`, which only validates that the ERC20 call returned `true` — it does not verify the actual amount received. When the loan token deducts a transfer fee, Midnight receives fewer tokens than `units` but records the full `units` as withdrawable, permanently breaking the invariant that `withdrawable <= actual loanToken balance`.

### Finding Description
**Code path:**

In `src/Midnight.sol` lines 508–520:
```solidity
position[id][onBehalf].debt -= UtilsLib.toUint128(units);      // L508
marketState[id].withdrawable += UtilsLib.toUint128(units);      // L509
// ...
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units); // L520
```

In `src/libraries/SafeTransferLib.sol` lines 24–34, `safeTransferFrom` only checks `success` and the boolean return value of `transferFrom`. It performs no pre/post balance snapshot and does not verify that `address(this)` received exactly `value` tokens.

**Root cause:** The accounting update at L509 uses the caller-supplied `units` parameter as the ground truth for how much was received, but the actual received amount for a fee-on-transfer token is `units * (1 - fee_rate)`. There is no balance-before/after check anywhere in `repay()`.

**Attacker inputs:**
- `market.loanToken` = a deployed fee-on-transfer ERC20 (e.g., 1% fee on every `transferFrom`)
- `units` = any positive repayment amount
- `callback` = `address(0)`, `data` = `""`
- `onBehalf` = the borrower (caller is authorized by default for themselves)

**Exploit flow:**
1. Market is created with a fee-on-transfer ERC20 as `loanToken` (no token whitelist exists).
2. Borrower has debt `D` in the market.
3. Borrower calls `repay(market, D, borrower, address(0), "")`.
4. L508: `position[id][borrower].debt -= D` → debt cleared.
5. L509: `marketState[id].withdrawable += D` → withdrawable increased by `D`.
6. L520: `safeTransferFrom(loanToken, borrower, address(this), D)` → token deducts 1%, so Midnight receives only `D * 0.99`.
7. `safeTransferFrom` returns `true` (fee-on-transfer tokens do not revert); no revert occurs.
8. Final state: `withdrawable == D` but `loanToken.balanceOf(address(midnight)) == D * 0.99`.

**Why existing checks fail:**
- `safeTransferFrom` only checks `success` and the boolean return — it cannot detect a fee deduction. [1](#0-0) 
- There is no token whitelist or fee-on-transfer guard at market creation or in `repay()`.
- The Certora spec `repayIncreasesWithdrawable` asserts `withdrawableAfter == withdrawableBefore + units` — this is proven against a model where the token is well-behaved, so it does not catch the fee-on-transfer case. [2](#0-1) 

### Impact Explanation
After each fee-on-transfer repayment, `marketState[id].withdrawable` exceeds the actual `loanToken` balance held by Midnight by `units * fee_rate`. This deficit accumulates with every repayment. When lenders call `withdraw()`, the function transfers `units` tokens out using `safeTransfer` — the last lenders to withdraw will encounter an ERC20 transfer failure (insufficient balance), permanently freezing their credit. The protocol becomes insolvent for that market: total lender credit > redeemable tokens. [3](#0-2) 

### Likelihood Explanation
- **Precondition:** The loan token must be a fee-on-transfer ERC20. No privileged action is required to create such a market — `touchMarket` imposes no token type restriction.
- **Feasibility:** Any borrower can trigger this on every `repay()` call. The deficit compounds with each repayment, making the impact proportional to total repayment volume.
- **Repeatability:** Every `repay()` call with a fee-on-transfer token widens the gap. The bug is deterministic and reproducible.

### Recommendation
Measure the actual received amount using a pre/post balance check inside `repay()`:

```solidity
uint256 balanceBefore = IERC20(market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
require(IERC20(market.loanToken).balanceOf(address(this)) - balanceBefore == units, FeeOnTransferToken());
```

Alternatively, enforce a token whitelist at market creation that excludes fee-on-transfer tokens. The same fix should be applied to `supplyCollateral` and `liquidate`, which also use `safeTransferFrom` without a received-amount check. [4](#0-3) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";

// Fee-on-transfer ERC20: deducts 1% on every transferFrom
contract FeeToken is ERC20 {
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount / 100;
        super.transferFrom(from, to, amount - fee); // only amount*(1-1%) arrives
        _burn(from, fee);                           // fee is destroyed
        return true;
    }
}

contract FeeOnTransferRepayTest is Test {
    Midnight midnight;
    FeeToken loanToken;
    Market market;
    bytes32 id;

    function setUp() public {
        loanToken = new FeeToken();
        // deploy midnight, create market with loanToken, lender supplies credit,
        // borrower takes debt of N units
    }

    function testFeeOnTransferInsolvency() public {
        uint256 units = 1000e18;
        // borrower repays full debt
        vm.prank(borrower);
        loanToken.approve(address(midnight), units);
        midnight.repay(market, units, borrower, address(0), "");

        uint256 withdrawable = midnight.withdrawable(id);
        uint256 actualBalance = loanToken.balanceOf(address(midnight));

        // ASSERTION: withdrawable > actual balance — invariant broken
        assertGt(withdrawable, actualBalance, "withdrawable exceeds actual balance");
        // withdrawable == 1000e18, actualBalance == 990e18 (1% fee deducted)

        // ASSERTION: lender cannot fully withdraw
        vm.prank(lender);
        vm.expectRevert(); // transfer fails — insufficient balance
        midnight.withdraw(market, units, lender, lender);
    }
}
```

Expected: `withdrawable` equals `1000e18` while `loanToken.balanceOf(address(midnight))` equals `990e18`; the subsequent `withdraw()` for the full amount reverts, proving lender insolvency. [5](#0-4)

### Citations

**File:** src/libraries/SafeTransferLib.sol (L27-33)
```text
        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transferFrom, (from, to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
        }
        require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferFromReturnedFalse());
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

**File:** src/Midnight.sol (L494-499)
```text
        _marketState.withdrawable -= UtilsLib.toUint128(units);
        _marketState.totalUnits -= UtilsLib.toUint128(units);

        emit EventsLib.Withdraw(msg.sender, id, units, onBehalf, receiver, pendingFeeDecrease);

        SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
```

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
