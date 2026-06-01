### Title
Fee-on-Transfer Loan Token Overcounts `claimableSettlementFee`, Enabling Insolvency via Settlement Fee Claim - (File: src/Midnight.sol)

### Summary
In `take()`, `claimableSettlementFee[loanToken]` is incremented by the full `buyerAssets - sellerAssets` spread before the inbound `safeTransferFrom` executes. `SafeTransferLib.safeTransferFrom` only checks call success and return value — it does not verify the actual received amount. When the loan token charges a transfer fee, Midnight receives less than the credited amount, permanently overcounting `claimableSettlementFee` relative to actual token holdings. The Certora solvency spec explicitly acknowledges this gap by assuming "no fee taking from sender or receiver" in its `CVL_transferFrom` model.

### Finding Description

**Exact code path:**

In `src/Midnight.sol`, `take()`:

```
// Step 1: accounting committed at full spread — BEFORE transfer
claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;  // line 418

// Step 2: actual inbound transfer — no received-amount check
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);  // line 455
```

`SafeTransferLib.safeTransferFrom` (`src/libraries/SafeTransferLib.sol` lines 24–34) calls `transferFrom(from, to, value)` and checks only that the call did not revert and returned `true`. It performs no balance-before/balance-after check. A fee-on-transfer token silently delivers `value * (1 - fee_rate)` to `address(this)` while returning `true`.

**Attacker-controlled inputs:**
- Market creator (unprivileged, permissionless) deploys a market whose `loanToken` is a fee-on-transfer ERC20.
- Taker (unprivileged) calls `take(offer, ratifierData, units, taker, receiver, address(0), "")` on a buy offer in that market.
- No callback is needed (`takerCallback = address(0)`), so `payer = buyer = offer.maker`.

**State change discrepancy after `take()`:**
- `claimableSettlementFee[loanToken]` increases by `D = buyerAssets - sellerAssets`
- Midnight's actual token balance increases by only `D * (1 - fee_rate)`
- Overcount per call: `D * fee_rate`

**Why existing checks fail:**
- `SafeTransferLib` has no received-amount verification.
- The Certora `tokenBalanceCorrect` strong invariant (`certora/specs/Solvency.spec` lines 162–163) is proven only under the explicit assumption that tokens transfer exactly the requested amount (lines 31–33: "no fee taking from sender or receiver"). The `pendingFeeReceipt` ghost correctly tracks the pre-transfer gap but is cleared only when the exact credited amount arrives — with a fee-on-transfer token, it is never fully cleared, breaking `pendingFeeReceiptZero` (line 157–158).
- No market-creation guard restricts fee-on-transfer tokens.

### Impact Explanation

After one or more `take()` calls with a fee-on-transfer loan token, `claimableSettlementFee[loanToken]` exceeds the actual fee tokens held by the contract. When the feeClaimer calls `claimSettlementFee(token, claimableSettlementFee[token], receiver)` (`src/Midnight.sol` lines 305–310), `safeTransfer` sends the overcounted amount out. The deficit is covered by tokens that belong to lenders (`withdrawable`) or collateral providers. Subsequent lender `withdraw()` calls revert due to insufficient balance — concrete insolvency.

### Likelihood Explanation

Preconditions: (1) a market exists with a fee-on-transfer loan token — achievable by any unprivileged user since market creation is permissionless; (2) at least one `take()` with non-zero `buyerAssets - sellerAssets` occurs. Both are trivially satisfiable. The overcount accumulates with every `take()`, making the discrepancy grow monotonically. The feeClaimer claiming the full `claimableSettlementFee` balance is a routine operation, not an adversarial one.

### Recommendation

Add a balance-before/balance-after check in `take()` for the inbound fee transfer, and credit only the actually received amount to `claimableSettlementFee`:

```solidity
uint256 balanceBefore = IERC20(offer.market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
uint256 received = IERC20(offer.market.loanToken).balanceOf(address(this)) - balanceBefore;
claimableSettlementFee[offer.market.loanToken] += received; // replace the pre-transfer increment
```

Alternatively, explicitly document and enforce (via a registry or allowlist) that only standard ERC20 tokens without transfer fees may be used as loan tokens, and add a revert guard in `touchMarket` or market creation.

### Proof of Concept

```solidity
// Foundry unit test outline
contract FeeToken is ERC20 {
    uint256 public constant FEE_BPS = 100; // 1% fee
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount * FEE_BPS / 10000;
        super.transferFrom(from, to, amount - fee); // deliver less
        _burn(from, fee);                           // fee destroyed
        return true;
    }
}

function testFeeOnTransferOvercountsSettlementFee() public {
    FeeToken feeToken = new FeeToken();
    // Create market with feeToken as loanToken (permissionless)
    Market memory market = buildMarket(address(feeToken));
    midnight.touchMarket(market);

    // Setup: lender makes buy offer, borrower has collateral
    Offer memory offer = buildBuyOffer(lender, market);
    collateralize(market, borrower, MAX_DEBT);

    uint256 balanceBefore = feeToken.balanceOf(address(midnight));
    uint256 claimableBefore = midnight.claimableSettlementFee(address(feeToken));

    // Taker (borrower) takes the buy offer
    vm.prank(borrower);
    (uint256 buyerAssets, uint256 sellerAssets) = midnight.take(offer, ratifierData, units, borrower, borrower, address(0), "");

    uint256 spread = buyerAssets - sellerAssets;
    uint256 actualReceived = feeToken.balanceOf(address(midnight)) - balanceBefore;
    uint256 claimableAfter = midnight.claimableSettlementFee(address(feeToken));

    // Assert: claimableSettlementFee overcounts actual received tokens
    assertEq(claimableAfter - claimableBefore, spread);          // credited full spread
    assertLt(actualReceived, spread);                             // but received less
    assertGt(claimableAfter - claimableBefore, actualReceived);  // OVERCOUNT confirmed

    // Assert: feeClaimer claiming full claimable drains lender withdrawable
    uint256 withdrawableBefore = midnight.marketState(id).withdrawable; // lender funds
    vm.prank(feeClaimer);
    midnight.claimSettlementFee(address(feeToken), claimableAfter, feeClaimer);

    // Lender withdraw now fails — balance insufficient
    vm.prank(lender);
    vm.expectRevert();
    midnight.withdraw(market, withdrawableBefore, lender, lender);
}
```

**Expected assertions:**
- `claimableSettlementFee[feeToken] - claimableBefore == spread` (full spread credited)
- `actualReceived < spread` (fee-on-transfer delivered less)
- After `claimSettlementFee(feeToken, claimableAfter, feeClaimer)`, lender `withdraw()` reverts [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** certora/specs/Solvency.spec (L155-163)
```text
// For any token, the pending settlement fee receipt after a transaction is 0: every claimableSettlementFee
// increment in take is paid back in by the same-function inbound transfer.
weak invariant pendingFeeReceiptZero(address token)
    pendingFeeReceipt[token] == 0;

// For any token, the balance of the contract is always greater than or equal to the sum of all collateral, withdrawable, and claimable settlement fee amounts for that token minus the flash loaned amount.
// Note: this invariant is strong, so it also holds before each external call.
strong invariant tokenBalanceCorrect(address token)
    tokenBalances[token][currentContract] >= collateralSum(token) + withdrawableSum(token) + claimableSettlementFee(token) - flashloans[token] - pendingFeeReceipt[token]
```
