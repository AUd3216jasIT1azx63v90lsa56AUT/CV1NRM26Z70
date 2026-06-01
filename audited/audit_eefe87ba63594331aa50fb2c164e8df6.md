### Title
Fee-on-Transfer Loan Token in `repay()` Inflates `withdrawable` Beyond Actual Balance Received - (`src/Midnight.sol`)

### Summary

`repay()` unconditionally increments `marketState[id].withdrawable` by the nominal `units` parameter before calling `SafeTransferLib.safeTransferFrom`, which only verifies the call succeeds and returns `true` but never checks the actual amount credited to the contract. When the loan token silently deducts a transfer fee, the contract records more withdrawable credit than tokens it actually holds, permanently violating the solvency invariant `balance >= collateralSum + withdrawableSum + claimableSettlementFee`. The formal Certora solvency proof explicitly assumes fee-free ERC20 transfers and therefore does not cover this case.

### Finding Description

**Exact code path — `src/Midnight.sol` lines 502–521:**

```
position[id][onBehalf].debt  -= units;          // line 508
marketState[id].withdrawable += units;           // line 509
// ... optional callback ...
SafeTransferLib.safeTransferFrom(               // line 520
    market.loanToken, payer, address(this), units
);
```

`SafeTransferLib.safeTransferFrom` (`src/libraries/SafeTransferLib.sol` lines 24–34) calls `transferFrom` and checks only that the call did not revert and returned `true`. It performs no balance-before / balance-after comparison. A fee-on-transfer token deducts `fee` from the transferred amount, so the contract receives `units - fee` while `withdrawable` was already raised by `units`.

**Market creation — `src/Midnight.sol` lines 754–791:**

`touchMarket()` validates collateral params, LLTV, maxLif, and maturity, but imposes **no restriction on the loan token type**. Any address, including a fee-on-transfer ERC20, is accepted as `market.loanToken`.

**Formal verification gap — `certora/specs/Solvency.spec` lines 31–33:**

```
// Assume ERC20 tokens transfer correctly: no fee taking from sender or receiver...
function _.transferFrom(...) external ... => CVL_transferFrom(...) expect(bool);
```

The `tokenBalanceCorrect` strong invariant (line 162) is proven only under this assumption. Fee-on-transfer tokens are outside the proof's scope.

**Exploit flow:**

1. Attacker (or any user) calls `touchMarket` with a fee-on-transfer token as `market.loanToken` — succeeds with no restriction.
2. A lender takes a buy offer; lender credit = `units`, contract holds the settlement-fee portion.
3. Borrower accumulates `units` of debt.
4. Borrower calls `repay(market, units, borrower, address(0), "")`.
   - `withdrawable` += `units` (line 509).
   - `safeTransferFrom` executes; token deducts `fee`; contract receives `units - fee`.
5. Invariant broken: `withdrawable` overstates actual balance by `fee`.
6. Lender calls `withdraw(market, units, lender, lender)`.
   - `safeTransfer(loanToken, lender, units)` is attempted.
   - Contract is short by `fee`; either the transfer reverts (lender cannot exit) or, if other markets share the same token and hold surplus, those funds are drained.
7. Step 4–6 is repeatable: each repay cycle widens the deficit by another `fee`.

### Impact Explanation

The solvency invariant `balance(loanToken, midnight) >= withdrawableSum(loanToken) + collateralSum(loanToken) + claimableSettlementFee(loanToken)` is violated after every `repay` call with a fee-on-transfer loan token. Lenders holding credit in the affected market cannot fully redeem their tokens. If multiple markets share the same fee-on-transfer loan token, the deficit can be covered temporarily by other markets' balances, allowing one lender to withdraw at the expense of others — a classic insolvency / bank-run scenario.

### Likelihood Explanation

**Preconditions:**
- A fee-on-transfer ERC20 is used as `market.loanToken`. Any unprivileged address can create such a market via `touchMarket`.
- A borrower repays any nonzero `units`.

**Feasibility:** Permissionless market creation means no admin action is required. Fee-on-transfer tokens (e.g., tokens with built-in tax mechanisms) exist on mainnet. The attacker need only be a borrower in such a market — a standard, unprivileged role.

**Repeatability:** Every `repay` call widens the deficit by `units * feeRate`. The effect is cumulative and unbounded.

### Recommendation

Record the contract's loan-token balance before and after the `safeTransferFrom` in `repay()` (and symmetrically in `liquidate()`), and use the measured delta — not the nominal `units` — to update `withdrawable` and reduce `debt`:

```solidity
uint256 balanceBefore = IERC20(market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
uint256 received = IERC20(market.loanToken).balanceOf(address(this)) - balanceBefore;

position[id][onBehalf].debt      -= UtilsLib.toUint128(received);
marketState[id].withdrawable     += UtilsLib.toUint128(received);
```

Alternatively, explicitly document and enforce that fee-on-transfer tokens are not supported as loan tokens (e.g., a registry or a balance-check gate in `touchMarket`).

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {IMidnight, Market, CollateralParams} from "src/interfaces/IMidnight.sol";

/// Minimal fee-on-transfer ERC20: deducts 1% on every transferFrom.
contract FeeToken {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    uint256 public constant FEE_BPS = 100; // 1 %

    function mint(address to, uint256 amt) external { balanceOf[to] += amt; }
    function approve(address sp, uint256 v) external returns (bool) {
        allowance[msg.sender][sp] = v; return true;
    }
    function transfer(address to, uint256 v) external returns (bool) {
        balanceOf[msg.sender] -= v; balanceOf[to] += v; return true;
    }
    function transferFrom(address from, address to, uint256 v) external returns (bool) {
        allowance[from][msg.sender] -= v;
        uint256 fee = v * FEE_BPS / 10_000;
        balanceOf[from] -= v;
        balanceOf[to]   += v - fee;   // receiver gets less
        return true;
    }
    function code() external view returns (bytes memory) { return address(this).code; }
}

contract FeeOnTransferRepayTest is Test {
    Midnight midnight;
    FeeToken loanToken;
    // ... standard test setup (oracle, collateral token, market params) ...

    function testFeeOnTransferBreaksSolvency() public {
        // 1. Setup: lender supplies credit of `units`, borrower has `units` debt.
        uint256 units = 1_000e18;
        // ... (take setup omitted for brevity) ...

        // 2. Borrower repays `units` with fee-on-transfer loan token.
        uint256 balBefore = loanToken.balanceOf(address(midnight));
        vm.prank(borrower);
        midnight.repay(market, units, borrower, address(0), "");
        uint256 balAfter = loanToken.balanceOf(address(midnight));

        uint256 actualReceived = balAfter - balBefore;          // units - fee
        uint256 withdrawableNow = midnight.withdrawable(id);    // units (nominal)

        // ASSERTION: withdrawable must not exceed actual balance increase.
        // This assertion FAILS, proving the invariant is broken.
        assertLe(
            withdrawableNow,
            balAfter,
            "INVARIANT VIOLATED: withdrawable > actual balance"
        );

        // FUZZ VARIANT: run with fee rates [1, 9999] bps and repay amounts [1, type(uint128).max].
        // assert sum(withdrawable across all markets for loanToken)
        //     <= loanToken.balanceOf(address(midnight))
        // after each repay call.
    }
}
```

**Expected assertion failure:** `withdrawableNow` equals `units` while `actualReceived` equals `units * 9900 / 10000`, so `withdrawableNow > balAfter`, confirming the invariant breach. A lender subsequently calling `withdraw(market, units, lender, lender)` will either revert (if no surplus exists) or drain tokens belonging to other markets.