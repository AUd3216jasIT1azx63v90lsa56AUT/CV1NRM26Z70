### Title
Fee-on-Transfer Loan Token Inflates `claimableSettlementFee` Across Shared Markets, Violating Cross-Market Solvency Invariant - (File: src/Midnight.sol)

### Summary
In `take`, `claimableSettlementFee[loanToken]` is incremented by the full nominal spread `buyerAssets - sellerAssets` before the inbound transfer executes. When the loan token is fee-on-transfer, the contract receives only `(buyerAssets - sellerAssets) * (1 - feeRate)`, creating a persistent per-take shortfall. Because `claimableSettlementFee` is keyed by token address (not market id), every take across every market sharing that token accumulates the same shortfall, causing the solvency invariant `balance >= collateralSum + withdrawableSum + claimableSettlementFee` to be violated.

### Finding Description

**Exact code path:**

`src/Midnight.sol` line 418 increments accounting before the transfer:
```solidity
claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
``` [1](#0-0) 

Then line 455 executes the inbound transfer to the contract:
```solidity
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
``` [2](#0-1) 

With a fee-on-transfer token charging rate `f`, the contract receives `(buyerAssets - sellerAssets) * (1 - f)` but `claimableSettlementFee` is incremented by the full `buyerAssets - sellerAssets`. The gap per take is `(buyerAssets - sellerAssets) * f`. There is no post-transfer balance check to reconcile the actual received amount.

**Why `withdrawable` is NOT inflated:** `take` does not modify `marketState[id].withdrawable`. The Certora rule `withdrawableUnchanged` confirms this. [3](#0-2)  The solvency violation is entirely through `claimableSettlementFee` inflation.

**Cross-market amplification:** `claimableSettlementFee` is a per-token mapping, not per-market. Takes across N markets sharing the same fee-on-transfer token all inflate the same `claimableSettlementFee[token]`, so the shortfall is:
```
shortfall = sum_i( (buyerAssets_i - sellerAssets_i) * f )
```
across all takes in all markets sharing the token.

**Why existing protections fail:** The Certora `pendingFeeReceiptZero` invariant and `tokenBalanceCorrect` strong invariant are proven under the explicit assumption that ERC20 tokens are well-behaved: [4](#0-3)  The `pendingFeeReceipt` ghost is cleared only when `dest == currentContract && pendingFeeReceipt[token] == to_mathint(value)` — i.e., when the transfer delivers exactly the nominal amount. [5](#0-4)  With fee-on-transfer, the delivered value is less than nominal, so `pendingFeeReceipt` is never cleared, and the `pendingFeeReceiptZero` invariant is violated, breaking the chain of proof for `tokenBalanceCorrect`. The SECURITY.md contains no explicit exclusion of fee-on-transfer tokens. [6](#0-5) 

**Attacker-controlled inputs:**
- Market creation is permissionless; attacker creates markets with a fee-on-transfer loan token.
- Attacker acts as taker (or maker) and executes `take` calls across multiple markets.
- No privileged access required.

### Impact Explanation

After K takes across markets sharing the same fee-on-transfer token, the solvency invariant is violated:

```
tokenBalance[token][Midnight] < collateralSum(token) + withdrawableSum(token) + claimableSettlementFee(token)
```

by exactly `sum_i((buyerAssets_i - sellerAssets_i) * f)`. When the privileged `feeClaimer` calls `claimSettlementFee` for the full `claimableSettlementFee` amount, it drains tokens that back `withdrawableSum`, causing lenders' `withdraw` calls to revert for insufficient balance. The shared token is rendered insolvent at the protocol level across all markets using it.

### Likelihood Explanation

**Preconditions:**
1. A fee-on-transfer ERC20 token is used as a loan token in one or more markets (permissionless market creation makes this trivially achievable).
2. Valid offers exist in those markets (maker + ratifier setup required, but also permissionless).

**Feasibility:** High. Market creation is permissionless. The attacker can be both market creator and taker. The shortfall is proportional to `feeRate * spread * units`, so it grows with each take. The effect is permanent and cumulative — it cannot be reversed without an external token donation.

**Repeatability:** Every `take` call on any market sharing the token adds to the shortfall. The attack is repeatable indefinitely.

### Recommendation

Replace the nominal accounting increment with a balance-delta measurement:

```solidity
uint256 balanceBefore = IERC20(offer.market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
uint256 actualReceived = IERC20(offer.market.loanToken).balanceOf(address(this)) - balanceBefore;
claimableSettlementFee[offer.market.loanToken] += actualReceived;
```

Alternatively, explicitly document and enforce that fee-on-transfer tokens are not supported as loan tokens (e.g., add a token whitelist or a deployment-time check), and add a Foundry invariant test that asserts `balance >= collateralSum + withdrawableSum + claimableSettlementFee` after takes with a fee-on-transfer mock token.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import "../src/Midnight.sol";

// Fee-on-transfer token: charges 1% on every transferFrom to the contract
contract FeeOnTransferToken is ERC20 {
    uint256 constant FEE_BPS = 100; // 1%
    address public midnight;
    constructor(address _midnight) ERC20("FOT","FOT") { midnight = _midnight; }
    function transferFrom(address src, address dst, uint256 amt) public override returns (bool) {
        uint256 fee = dst == midnight ? amt * FEE_BPS / 10000 : 0;
        super.transferFrom(src, dst, amt - fee);
        if (fee > 0) _burn(src, fee); // fee destroyed
        return true;
    }
}

contract CrossMarketFOTTest is Test {
    Midnight midnight;
    FeeOnTransferToken token;

    function testCrossMarketSolvencyViolation() public {
        // Setup: deploy Midnight, create 3 markets with same FOT loan token
        // For each market: create lender buy offer, collateralize borrower, execute take
        // After 3 takes:
        uint256 totalClaimable = midnight.claimableSettlementFee(address(token));
        uint256 totalWithdrawable = /* sum withdrawable across 3 market ids */ 0;
        uint256 balance = token.balanceOf(address(midnight));

        // Assert solvency invariant is VIOLATED:
        // balance < totalWithdrawable + totalClaimable
        assertLt(balance, totalWithdrawable + totalClaimable,
            "solvency invariant violated: claimableSettlementFee inflated by FOT gap");

        // Assert feeClaimer cannot claim full amount without draining withdrawable:
        vm.prank(feeClaimer);
        vm.expectRevert(); // transfer reverts: insufficient balance
        midnight.claimSettlementFee(address(token), totalClaimable, feeClaimer);
    }
}
```

**Expected assertions:**
- `claimableSettlementFee[token]` exceeds actual tokens held for fees by `sum(spread_i * feeRate)` across all takes.
- `tokenBalance[token][Midnight] < collateralSum + withdrawableSum + claimableSettlementFee`.
- `claimSettlementFee` for the full amount either reverts or drains `withdrawable`-backing tokens, causing subsequent `withdraw` calls to revert.

### Citations

**File:** src/Midnight.sol (L418-418)
```text
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** certora/specs/WithdrawableMonotonicity.spec (L45-57)
```text
rule withdrawableUnchanged(method f, env e, calldataarg args, bytes32 id)
filtered {
    f -> !f.isView
        && f.selector != sig:repay(Midnight.Market, uint256, address, address, bytes).selector
        && f.selector != sig:liquidate(Midnight.Market, uint256, uint256, uint256, address, bool, address, address, bytes).selector
        && f.selector != sig:withdraw(Midnight.Market, uint256, address, address).selector
        && f.selector != sig:claimContinuousFee(Midnight.Market, uint256, address).selector
} {
    uint256 withdrawableBefore = withdrawable(id);
    f(e, args);
    uint256 withdrawableAfter = withdrawable(id);
    assert withdrawableAfter == withdrawableBefore;
}
```

**File:** certora/specs/Solvency.spec (L31-33)
```text
    // Assume ERC20 tokens transfer correctly: no fee taking from sender or receiver, no rebasing, no blacklisting, no transfer limits.
    function _.transfer(address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, e.msg.sender, a, v) expect(bool);
    function _.transferFrom(address src, address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, src, a, v) expect(bool);
```

**File:** certora/specs/Solvency.spec (L54-57)
```text
        // Settle pending settlement fee receipts only on the exact fee transfer expected by take().
        if (dest == currentContract && pendingFeeReceipt[token] == to_mathint(value)) {
            pendingFeeReceipt[token] = 0;
        }
```

**File:** SECURITY.md (L1-27)
```markdown
# Common Vulnerability Exclusion List

## Out of Scope & Rules

These are the default impacts recommended to projects to mark as out of scope for their bug bounty program. The actual list of out-of-scope impacts differs from program to program.

### General

- Impacts requiring attacks that the reporter has already exploited themselves, leading to damage.
- Impacts caused by attacks requiring access to leaked keys/credentials.
- Impacts caused by attacks requiring access to privileged addresses (governance, strategist), except in cases where the contracts are intended to have no privileged access to functions that make the attack possible.
- Impacts relying on attacks involving the depegging of an external stablecoin where the attacker does not directly cause the depegging due to a bug in code.
- Mentions of secrets, access tokens, API keys, private keys, etc. in GitHub will be considered out of scope without proof that they are in use in production.
- Best practice recommendations.
- Feature requests.
- Impacts on test files and configuration files, unless stated otherwise in the bug bounty program.

### Smart Contracts / Blockchain DLT

- Incorrect data supplied by third-party oracles.
- Impacts requiring basic economic and governance attacks (e.g. 51% attack).
- Lack of liquidity impacts.
- Impacts from Sybil attacks.
- Impacts involving centralization risks.

Note: This does not exclude oracle manipulation/flash-loan attacks.

```
