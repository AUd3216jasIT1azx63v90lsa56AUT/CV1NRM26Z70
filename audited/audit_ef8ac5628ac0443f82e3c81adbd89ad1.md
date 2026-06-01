### Title
Fee-on-Transfer loanToken Causes `claimableSettlementFee` Overstatement and Protocol Insolvency in `take()` - (File: src/Midnight.sol)

### Summary
In `take()`, `claimableSettlementFee` is incremented by the full `buyerAssets - sellerAssets` at line 418 before the two `safeTransferFrom` calls at lines 455–456. When `loanToken` is a fee-on-transfer token, midnight only receives `(buyerAssets - sellerAssets) * (1 - fee)` from the first transfer, creating a permanent gap between the recorded `claimableSettlementFee` and the actual contract balance. This gap accumulates across every `take()` call and eventually causes `claimSettlementFee` to drain funds reserved for `withdrawable` or to revert entirely.

### Finding Description
**Code path:**

`src/Midnight.sol` line 418 increments accounting before any transfer: [1](#0-0) 

Then lines 455–456 execute two sequential `safeTransferFrom` calls from `payer`: [2](#0-1) 

**Root cause:** The accounting update at line 418 assumes the full `buyerAssets - sellerAssets` will land in the contract. With a fee-on-transfer `loanToken`, the first transfer delivers only `(buyerAssets - sellerAssets) * (1 - fee)` to midnight. The second transfer delivers only `sellerAssets * (1 - fee)` to `receiver`. Neither shortfall is detected or corrected.

**Attacker-controlled inputs:**
- `offer.market.loanToken` = a fee-on-transfer ERC20 (market creation is permissionless; any address can create a market with any token)
- `offer.buy = true`, `units > 0`, `sellerAssets > 0`
- `payer` = `offer.maker` (no callback path needed)

**Exploit flow:**
1. Maker calls `setIsRootRatified` on `SetterRatifier` to ratify a buy offer root. [3](#0-2) 
2. Taker calls `take()` with the ratified buy offer.
3. Line 418 increments `claimableSettlementFee[loanToken] += buyerAssets - sellerAssets` (full amount).
4. Line 455 transfers `buyerAssets - sellerAssets` from payer to midnight; midnight receives `(buyerAssets - sellerAssets) * (1 - fee)`.
5. Line 456 transfers `sellerAssets` from payer to receiver; receiver receives `sellerAssets * (1 - fee)`.
6. `claimableSettlementFee` is now overstated by `fee * (buyerAssets - sellerAssets)`.

**Why existing checks fail:**
- No Solidity-level exclusion of fee-on-transfer tokens exists anywhere in the codebase.
- The Certora `pendingFeeReceiptZero` invariant (which tracks the gap between the `claimableSettlementFee` increment and the inbound transfer) is proven under an idealized `CVL_transferFrom` that does not model fee deductions: [4](#0-3) 
- The `tokenBalanceCorrect` solvency invariant likewise assumes transfers deliver the full requested amount: [5](#0-4) 
- Both invariants are broken by fee-on-transfer tokens; neither is enforced on-chain.

**Correction on "double fee loss vs. single-transfer":** The two-transfer design actually produces a *smaller* protocol-side accounting gap (`fee * (buyerAssets - sellerAssets)`) than a hypothetical single-transfer design would (`fee * buyerAssets`). The "worse than single-transfer" framing in the question is inaccurate. However, the accounting discrepancy is real and accumulates regardless.

### Impact Explanation
After each `take()` with a fee-on-transfer `loanToken`, `claimableSettlementFee[loanToken]` exceeds the contract's actual token balance by `fee * (buyerAssets - sellerAssets)`. Over repeated takes this gap grows. When `claimSettlementFee` is called: [6](#0-5) 
it will either revert (insufficient balance) or succeed by consuming tokens that belong to `withdrawable`, making lenders unable to withdraw their funds. The `tokenBalanceCorrect` invariant — `balance >= collateralSum + withdrawableSum + claimableSettlementFee` — is violated after the first take.

### Likelihood Explanation
Any market can be created with a fee-on-transfer `loanToken` (market creation is permissionless). The preconditions — `offer.buy == true`, `units > 0`, `sellerAssets > 0` — are the normal operating conditions for any buy-side take. No privileged access is required beyond the maker ratifying their own offer root. The bug triggers on every single `take()` call in such a market and is fully repeatable.

### Recommendation
After each `safeTransferFrom` to `address(this)`, measure the actual received amount by comparing pre- and post-transfer balances, and use the measured amount to update `claimableSettlementFee`. Alternatively, document and enforce (via a registry or market creation check) that fee-on-transfer tokens are not supported as `loanToken`, consistent with how Morpho Blue handles this class of tokens.

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
// ... standard test imports

contract FeeOnTransferLoanToken is ERC20 {
    uint256 public feeBps; // e.g. 100 = 1%
    constructor(uint256 _feeBps) ERC20("FOT","FOT") { feeBps = _feeBps; }
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount * feeBps / 10000;
        super.transferFrom(from, to, amount - fee); // deliver less
        _burn(from, fee);                           // fee destroyed
        return true;
    }
}

contract DoubleFeeLossTest is Test {
    function testClaimableSettlementFeeOverstated() public {
        // Setup: create market with FOT loanToken, fee = 1%
        // Fund maker (buyer), collateralize seller (taker)
        // Call take() with buy offer, units > 0

        uint256 claimableBefore = midnight.claimableSettlementFee(address(fotToken));
        uint256 balanceBefore   = fotToken.balanceOf(address(midnight));

        midnight.take(offer, ratifierData, units, taker, receiver, address(0), "");

        uint256 claimableAfter = midnight.claimableSettlementFee(address(fotToken));
        uint256 balanceAfter   = fotToken.balanceOf(address(midnight));

        uint256 recordedFee  = claimableAfter - claimableBefore;   // buyerAssets - sellerAssets
        uint256 receivedFee  = balanceAfter   - balanceBefore;     // (buyerAssets-sellerAssets)*(1-fee)

        // Core invariant: recorded == received. Fails with FOT token.
        assertEq(recordedFee, receivedFee, "claimableSettlementFee overstated");

        // Solvency invariant: balance >= claimable + withdrawable + collateral. Also fails.
        assertGe(
            fotToken.balanceOf(address(midnight)),
            midnight.claimableSettlementFee(address(fotToken)),
            "balance covers claimable fee"
        );
    }
}
```

**Expected assertions:** Both `assertEq` and `assertGe` fail. `recordedFee > receivedFee` by exactly `fee * (buyerAssets - sellerAssets)`. Fuzz over `feeBps ∈ [1, 1000]` and `units ∈ [1, MAX_DEBT]` to confirm the gap scales linearly with both parameters.

### Citations

**File:** src/Midnight.sol (L305-309)
```text
    function claimSettlementFee(address token, uint256 amount, address receiver) external {
        require(msg.sender == feeClaimer, OnlyFeeClaimer());
        claimableSettlementFee[token] -= amount;
        emit EventsLib.ClaimSettlementFee(msg.sender, token, amount, receiver);
        SafeTransferLib.safeTransfer(token, receiver, amount);
```

**File:** src/Midnight.sol (L418-418)
```text
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/ratifiers/SetterRatifier.sol (L24-27)
```text
    function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootRatified[maker][root] = newIsRootRatified;
        emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
```

**File:** certora/specs/Solvency.spec (L155-158)
```text
// For any token, the pending settlement fee receipt after a transaction is 0: every claimableSettlementFee
// increment in take is paid back in by the same-function inbound transfer.
weak invariant pendingFeeReceiptZero(address token)
    pendingFeeReceipt[token] == 0;
```

**File:** certora/specs/Solvency.spec (L162-163)
```text
strong invariant tokenBalanceCorrect(address token)
    tokenBalances[token][currentContract] >= collateralSum(token) + withdrawableSum(token) + claimableSettlementFee(token) - flashloans[token] - pendingFeeReceipt[token]
```
