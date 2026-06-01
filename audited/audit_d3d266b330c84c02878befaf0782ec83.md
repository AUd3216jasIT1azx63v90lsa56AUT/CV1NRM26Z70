### Title
`claimableSettlementFee` overcounted on fee-on-transfer loan token in `take()` - (File: src/Midnight.sol)

### Summary
In `take()`, `claimableSettlementFee[offer.market.loanToken]` is incremented by the nominal `buyerAssets - sellerAssets` at line 418 before the actual transfer at line 455. When the loan token is a fee-on-transfer ERC20, the contract receives strictly less than `buyerAssets - sellerAssets`, but the accounting is never reconciled. No balance check exists anywhere in the function to verify the actual received amount, so `claimableSettlementFee` permanently overstates the real token balance attributable to settlement fees.

### Finding Description
**Code path:** [1](#0-0) 

```
claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

This write occurs unconditionally at line 418, before any external call. The actual inbound transfer happens later: [2](#0-1) 

```
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
```

With a fee-on-transfer token, `safeTransferFrom` succeeds (it only checks for revert, not received amount), but the contract receives `buyerAssets - sellerAssets - fee`. There is no `balanceOf` snapshot before/after the transfer, and no other check in `take()` that would detect the shortfall.

**Attacker-controlled inputs:**
- Deploy a fee-on-transfer ERC20 as `loanToken` (attacker controls the token, or uses an existing one).
- Create a market with this token (permissionless via `touchMarket`).
- Post a buy offer (`offer.buy == true`) as maker.
- Call `take()` as an unprivileged taker with any valid `units`.

**Why existing checks fail:**
- `touchMarket` / market creation performs no token validation.
- `safeTransferFrom` from `SafeTransferLib` only reverts on call failure, not on received-amount shortfall.
- No pre/post balance check exists anywhere in `take()`.
- The `claimSettlementFee` function at lines 305–310 simply subtracts from `claimableSettlementFee` and transfers — it trusts the mapping is accurate. [3](#0-2) 

### Impact Explanation
After each `take()` with a fee-on-transfer loan token, `claimableSettlementFee[loanToken]` exceeds the actual token balance held by the contract attributable to settlement fees. The core solvency invariant — that contract balances cover all claimable fees plus withdrawable assets — is broken. When `feeClaimer` calls `claimSettlementFee` for the full overcounted amount, the transfer either reverts (DoS) or, if other loanToken balance exists in the contract (from deposits, repayments, etc.), silently drains funds belonging to lenders or borrowers.

### Likelihood Explanation
Fee-on-transfer tokens are a well-known ERC20 variant (e.g., USDT with fees enabled, STA, PAXG). Since market creation is permissionless and `touchMarket` imposes no token restrictions, any unprivileged actor can create a market with such a token. The exploit is repeatable on every `take()` call in that market, and the overcounting compounds with each trade.

### Recommendation
Record the contract's token balance before and after the transfer and use the actual received delta to update `claimableSettlementFee`:

```solidity
uint256 balanceBefore = IERC20(offer.market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
uint256 actualReceived = IERC20(offer.market.loanToken).balanceOf(address(this)) - balanceBefore;
// Then use actualReceived instead of (buyerAssets - sellerAssets) in the accounting above.
```

Alternatively, document that fee-on-transfer tokens are explicitly unsupported and add a revert guard (e.g., assert `actualReceived == buyerAssets - sellerAssets`).

### Proof of Concept
```solidity
// Foundry unit test outline
function test_claimableSettlementFeeOvercountedFeeOnTransfer() public {
    // 1. Deploy a fee-on-transfer ERC20 (1% fee on transferFrom)
    FeeToken token = new FeeToken(1e16); // 1% fee

    // 2. Create market with token as loanToken
    Market memory market = Market({ loanToken: address(token), ... });
    midnight.touchMarket(market);

    // 3. Maker posts a buy offer
    Offer memory offer = Offer({ buy: true, ... });

    // 4. Record state before take
    uint256 feeBefore = midnight.claimableSettlementFee(address(token));
    uint256 balanceBefore = token.balanceOf(address(midnight));

    // 5. Taker calls take()
    (uint256 buyerAssets, uint256 sellerAssets) = midnight.take(offer, ...);

    // 6. Assertions
    uint256 feeAfter = midnight.claimableSettlementFee(address(token));
    uint256 balanceAfter = token.balanceOf(address(midnight));

    uint256 accountedFee = feeAfter - feeBefore;          // = buyerAssets - sellerAssets
    uint256 actualReceived = balanceAfter - balanceBefore; // < buyerAssets - sellerAssets

    // This assertion FAILS, proving the bug:
    assertLe(accountedFee, actualReceived, "claimableSettlementFee overcounted");
}
```

Expected: `accountedFee > actualReceived` — the assertion fails, confirming `claimableSettlementFee` is overcounted relative to the actual balance increase.

### Citations

**File:** src/Midnight.sol (L305-310)
```text
    function claimSettlementFee(address token, uint256 amount, address receiver) external {
        require(msg.sender == feeClaimer, OnlyFeeClaimer());
        claimableSettlementFee[token] -= amount;
        emit EventsLib.ClaimSettlementFee(msg.sender, token, amount, receiver);
        SafeTransferLib.safeTransfer(token, receiver, amount);
    }
```

**File:** src/Midnight.sol (L418-418)
```text
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

**File:** src/Midnight.sol (L455-455)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
```
