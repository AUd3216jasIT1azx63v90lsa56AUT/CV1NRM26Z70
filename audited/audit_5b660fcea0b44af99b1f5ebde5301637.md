### Title
Fee-on-Transfer Loan Token Causes `claimableSettlementFee` Overstatement in `take()` - (File: src/Midnight.sol)

### Summary

`take()` increments `claimableSettlementFee` by the full computed `buyerAssets - sellerAssets` before executing two `safeTransferFrom` calls. When the loan token is fee-on-transfer, the contract receives only `(buyerAssets - sellerAssets) * (1 - fee_rate)`, but the accounting records the full amount. This permanently overstates `claimableSettlementFee` relative to actual token holdings, breaking the solvency invariant that the contract balance covers all claimable settlement fees.

### Finding Description

The exact code path in `src/Midnight.sol`:

**Line 418** — accounting update (before any transfer):
```solidity
claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
``` [1](#0-0) 

**Lines 455–456** — two separate `safeTransferFrom` calls from `payer`:
```solidity
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
``` [2](#0-1) 

`SafeTransferLib.safeTransferFrom` is a thin wrapper that only checks for code existence and a non-false return value — it performs no balance-before/after check and has no awareness of fee-on-transfer behavior. [3](#0-2) 

**Root cause:** The protocol computes `buyerAssets` and `sellerAssets` purely from `mulDivDown`/`mulDivUp` arithmetic on `units` and prices, then credits `claimableSettlementFee` with the arithmetic result. There is no balance snapshot before/after the transfer to reconcile what was actually received. [4](#0-3) 

**Exploit flow:**
1. Attacker (market creator, unprivileged) deploys a 1% fee-on-transfer ERC20 and creates a Midnight market with it as `loanToken`.
2. Any taker calls `take()` with nonzero `sellerAssets`.
3. Transfer 1 (`payer → address(this)`, amount = `buyerAssets - sellerAssets`): Midnight receives `(buyerAssets - sellerAssets) * 0.99`. The fee `(buyerAssets - sellerAssets) * 0.01` is burned/redirected.
4. Transfer 2 (`payer → receiver`, amount = `sellerAssets`): receiver receives `sellerAssets * 0.99`.
5. `claimableSettlementFee` was already incremented by the full `buyerAssets - sellerAssets` at step (line 418), so it is now overstated by `(buyerAssets - sellerAssets) * 0.01`.

**Why existing checks fail:** The Certora `tokenBalanceCorrect` solvency invariant is only proven under the explicit assumption that tokens transfer without fees:
```
// Assume ERC20 tokens transfer correctly: no fee taking from sender or receiver, no rebasing, no blacklisting, no transfer limits.
``` [5](#0-4) 

This is a verification assumption, not a runtime guard. No `require` in `take()` or anywhere in `Midnight.sol` prevents a fee-on-transfer token from being used as `loanToken`. The `pendingFeeReceipt` ghost in the Certora spec (which tracks the gap between the accounting increment and the actual transfer) would never be cleared to zero with a fee-on-transfer token, confirming the invariant break. [6](#0-5) 

### Impact Explanation

`claimableSettlementFee[loanToken]` accumulates a deficit of `(buyerAssets - sellerAssets) * fee_rate` per `take()` call. The `tokenBalanceCorrect` invariant — `balance >= collateralSum + withdrawableSum + claimableSettlementFee` — is violated. [7](#0-6) 

When `claimSettlementFee` is eventually called, the contract will attempt to transfer more tokens than it holds, causing a revert or, if the deficit is covered by other token inflows (e.g., repayments), silently draining funds that should cover `withdrawable` or collateral obligations. Repeated `take()` calls compound the deficit linearly.

### Likelihood Explanation

**Preconditions:** A market must exist with a fee-on-transfer loan token. Market creation is permissionless (unprivileged market creator). The taker only needs to call `take()` with any nonzero `sellerAssets` (i.e., any sell offer or buy offer with nonzero settlement fee). This is the normal operating path of the protocol.

**Feasibility:** High — any market creator can deploy this scenario. The fee-on-transfer token need not be malicious in the traditional sense; many real tokens (e.g., USDT with fees enabled, STA, PAXG) have or have had transfer fees.

**Repeatability:** Every `take()` call on such a market compounds the deficit. The bug is deterministic and repeatable.

### Recommendation

Use a balance-before/after pattern for the inbound transfer to Midnight, and credit `claimableSettlementFee` only with the amount actually received:

```solidity
uint256 balanceBefore = IERC20(offer.market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
uint256 actualReceived = IERC20(offer.market.loanToken).balanceOf(address(this)) - balanceBefore;
claimableSettlementFee[offer.market.loanToken] += actualReceived;
```

Move the `claimableSettlementFee` increment to after the transfer and use `actualReceived`. Alternatively, explicitly document and enforce (via a registry or `require`) that only standard ERC20 tokens with no transfer fees may be used as `loanToken`.

### Proof of Concept

**Foundry unit test plan:**

```solidity
// 1. Deploy FeeToken: 1% fee-on-transfer ERC20 (fee burned on every transferFrom).
// 2. Create Midnight market with loanToken = FeeToken.
// 3. Setup: lender supplies credit, borrower supplies collateral.
// 4. Record: uint256 midnightBalBefore = feeToken.balanceOf(address(midnight));
//            uint256 claimableBefore = midnight.claimableSettlementFee(address(feeToken));
// 5. Taker calls take() with units such that sellerAssets > 0 and buyerAssets - sellerAssets > 0.
//    Let fee = buyerAssets - sellerAssets.
// 6. Assertions:
//    assertEq(feeToken.balanceOf(address(midnight)), midnightBalBefore + fee * 99 / 100);
//    // accounting shows full amount:
//    assertEq(midnight.claimableSettlementFee(address(feeToken)), claimableBefore + fee);
//    // gap = fee * 1 / 100 > 0 → invariant violated:
//    assert(feeToken.balanceOf(address(midnight)) < claimableSettlementFee + withdrawableSum + collateralSum);
// 7. Fuzz variant: bound(feePct, 1, 50), bound(units, 1, maxUnits), assert gap == fee * feePct / 100.
```

Expected: the balance assertion at step 6 line 1 passes (Midnight received less), the accounting assertion at step 6 line 2 passes (full amount credited), and the solvency assertion at step 6 line 3 fails — confirming the invariant violation. [1](#0-0) [2](#0-1)

### Citations

**File:** src/Midnight.sol (L363-364)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
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

**File:** certora/specs/Solvency.spec (L140-158)
```text
// Settlement fee receipts pending settlement: claimableSettlementFee is incremented in take before
// the inbound fee transfer happens, so we track the gap and clear it in CVL_transferFrom.
persistent ghost mapping(address => mathint) pendingFeeReceipt {
    init_state axiom (forall address token. pendingFeeReceipt[token] == 0);
}

hook Sstore claimableSettlementFee[KEY address token] uint256 newVal (uint256 oldVal) {
    // Except for claimSettlementFee, the claimableSettlementFee is non-decreasing, see WithdrawableMonotonicity.spec.
    if (newVal > oldVal) {
        pendingFeeReceipt[token] = pendingFeeReceipt[token] + newVal - oldVal;
    }
}

/// INVARIANTS AND RULES ///

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
