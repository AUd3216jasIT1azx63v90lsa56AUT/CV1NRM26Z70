### Title
Fee-on-Transfer `loanToken` Overcredits `claimableSettlementFee` Relative to Actual Balance - (`src/Midnight.sol`)

### Summary

In `take()`, `claimableSettlementFee[offer.market.loanToken]` is incremented by the full nominal spread `buyerAssets - sellerAssets` at line 418, before the actual `safeTransferFrom` at line 455. When `loanToken` is a fee-on-transfer token, only `(buyerAssets - sellerAssets) * (1 - fee%)` tokens actually arrive at the contract, creating a permanent gap between the accounting entry and the real balance. Since `touchMarket` imposes no restriction on `loanToken`, any unprivileged market creator can trigger this condition.

### Finding Description

**Exact code path (`src/Midnight.sol`):**

```
line 418: claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
...
line 455: SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
line 456: SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

The accounting write at line 418 happens unconditionally before the transfer at line 455. For a standard ERC20 these are equal. For a fee-on-transfer token with fee rate `f`, the contract receives `(buyerAssets - sellerAssets) * (1 - f)` but `claimableSettlementFee` is credited the full `buyerAssets - sellerAssets`. The deficit per `take()` call is `(buyerAssets - sellerAssets) * f`.

**Why no existing check stops it:**

`touchMarket()` (lines 755–791) validates maturity, collateral params, LLTV tiers, and sort order, but performs **zero validation on `loanToken`** — any address is accepted. There is no token whitelist, no `transferFrom`-and-verify pattern, and no balance-before/after check.

The Certora formal verification in `certora/specs/Role.spec` lines 22–24 explicitly models `safeTransferFrom` as an exact-amount transfer (`cvlSafeTransferFrom` adds exactly `amount` to the recipient's ghost balance). The `tokenBalanceCorrect` strong invariant in `certora/specs/Solvency.spec` lines 162–163 and the `pendingFeeReceiptZero` weak invariant at line 157 are both proven under this assumption. Fee-on-transfer tokens are explicitly outside the model's scope, so the formal proofs provide no protection here.

**Attacker-controlled inputs:**
- Deploy a 1% fee-on-transfer ERC20
- Call `touchMarket(market)` with `market.loanToken = address(feeToken)` — permissionless
- Act as taker (or induce any taker) to call `take()` on an offer in that market

**State after each `take()`:**
- `claimableSettlementFee[feeToken]` increases by `spread`
- Contract balance increases by `spread * 0.99`
- Deficit accumulates: `claimableSettlementFee[feeToken] > balanceOf(feeToken) - (collateral + withdrawable)`

### Impact Explanation

When `feeClaimer` calls `claimSettlementFee(feeToken, claimableSettlementFee[feeToken], receiver)`, the `safeTransfer` at line 309 will either:
1. **Revert** — if the contract's total `feeToken` balance is less than `claimableSettlementFee[feeToken]`, the fee claimer cannot withdraw the full recorded amount; or
2. **Drain collateral/withdrawable buckets** — if other `feeToken` is present (from lenders' deposits or borrowers' collateral in the same token), the transfer succeeds by consuming tokens that belong to other users, breaking the core solvency invariant `balance >= collateral + withdrawable + claimableSettlementFee`.

Both outcomes constitute protocol insolvency for the affected token.

### Likelihood Explanation

- **Preconditions:** Deploy any fee-on-transfer ERC20 (trivial) and create a market with it (permissionless via `touchMarket`).
- **Feasibility:** No privileged role, no oracle manipulation, no user mistake required. Any taker interacting with the market triggers the accounting gap.
- **Repeatability:** Every `take()` call on the market widens the deficit by `spread * fee_rate`. The effect compounds across all takes in the market's lifetime.

### Recommendation

Replace the pre-transfer accounting increment with a balance-before/after pattern:

```solidity
uint256 balanceBefore = IERC20(offer.market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
uint256 received = IERC20(offer.market.loanToken).balanceOf(address(this)) - balanceBefore;
claimableSettlementFee[offer.market.loanToken] += received;
```

Alternatively, document that fee-on-transfer tokens are unsupported and add a token registry/whitelist enforced in `touchMarket`.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {IMidnight, Market, Offer, CollateralParams} from "src/interfaces/IMidnight.sol";

// 1% fee-on-transfer token
contract FeeToken is ERC20 {
    constructor() ERC20("Fee", "FEE") { _mint(msg.sender, 1e30); }
    function _transfer(address from, address to, uint256 amount) internal override {
        uint256 fee = amount / 100;
        super._transfer(from, to, amount - fee);
        // fee stays with sender (burned for simplicity)
    }
}

contract FeeOnTransferPoC is Test {
    Midnight midnight;
    FeeToken feeToken;
    address feeClaimer = address(0xFEE);

    function setUp() public { /* deploy midnight, set feeClaimer */ }

    function testClaimableSettlementFeeExceedsBalance() public {
        // 1. Create market with fee-on-transfer loanToken
        Market memory market; // populate collateralParams, maturity, loanToken = address(feeToken)
        midnight.touchMarket(market);

        // 2. Execute take() — taker pays buyerAssets, contract receives (buyerAssets - sellerAssets)*0.99
        // ... setup offer, collateral, approvals ...
        (uint256 buyerAssets, uint256 sellerAssets) = midnight.take(/* ... */);
        uint256 spread = buyerAssets - sellerAssets;

        // 3. Assert invariant violation
        uint256 claimable = midnight.claimableSettlementFee(address(feeToken));
        uint256 balance = feeToken.balanceOf(address(midnight));
        // claimable == spread, but balance increased by spread * 0.99
        assertGt(claimable, balance, "claimableSettlementFee exceeds actual balance");

        // 4. Assert fee claimer cannot withdraw full amount
        vm.prank(feeClaimer);
        vm.expectRevert(); // insufficient balance
        midnight.claimSettlementFee(address(feeToken), claimable, feeClaimer);
    }
}
```

**Expected assertions:**
- `claimableSettlementFee[feeToken] == spread` (e.g., 100)
- `feeToken.balanceOf(midnight) - priorBalance == spread * 99 / 100` (e.g., 99)
- `claimSettlementFee(feeToken, 100, feeClaimer)` reverts or drains other users' funds [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** src/Midnight.sol (L755-791)
```text
    function touchMarket(Market memory market) public returns (bytes32) {
        bytes32 id = toId(market);
        if (marketState[id].tickSpacing == 0) {
            require(market.maturity <= block.timestamp + 100 * 365 days, MaturityTooFar());
            require(market.collateralParams.length > 0, NoCollateralParams());
            require(market.collateralParams.length <= MAX_COLLATERALS, TooManyCollateralParams());
            address previousCollateralToken;
            for (uint256 i = 0; i < market.collateralParams.length; i++) {
                address collateralToken = market.collateralParams[i].token;
                require(collateralToken > previousCollateralToken, CollateralParamsNotSorted());
                uint256 lltv = market.collateralParams[i].lltv;
                require(isLltvAllowed(lltv), LltvNotAllowed());
                require(
                    market.collateralParams[i].maxLif == maxLif(lltv, LIQUIDATION_CURSOR_LOW)
                        || market.collateralParams[i].maxLif == maxLif(lltv, LIQUIDATION_CURSOR_HIGH),
                    InvalidMaxLif()
                );
                previousCollateralToken = collateralToken;
            }

            MarketState storage _marketState = marketState[id];
            _marketState.tickSpacing = DEFAULT_TICK_SPACING;
            uint16[7] memory _defaultSettlementFeeCbp = defaultSettlementFeeCbp[market.loanToken];
            _marketState.settlementFeeCbp0 = _defaultSettlementFeeCbp[0];
            _marketState.settlementFeeCbp1 = _defaultSettlementFeeCbp[1];
            _marketState.settlementFeeCbp2 = _defaultSettlementFeeCbp[2];
            _marketState.settlementFeeCbp3 = _defaultSettlementFeeCbp[3];
            _marketState.settlementFeeCbp4 = _defaultSettlementFeeCbp[4];
            _marketState.settlementFeeCbp5 = _defaultSettlementFeeCbp[5];
            _marketState.settlementFeeCbp6 = _defaultSettlementFeeCbp[6];
            _marketState.continuousFee = defaultContinuousFee[market.loanToken];
            IdLib.storeInCode(market, INITIAL_CHAIN_ID);

            emit EventsLib.MarketCreated(market, id);
        }
        return id;
    }
```

**File:** certora/specs/Role.spec (L22-24)
```text
    // Assume that tokens do not reenter and do not revert: this is justified as we verify properties about the function's bodies.
    function SafeTransferLib.safeTransfer(address token, address receiver, uint256 amount) internal => cvlSafeTransfer(token, receiver, amount);
    function SafeTransferLib.safeTransferFrom(address token, address from, address to, uint256 amount) internal => cvlSafeTransferFrom(token, from, to, amount);
```

**File:** certora/specs/Solvency.spec (L157-163)
```text
weak invariant pendingFeeReceiptZero(address token)
    pendingFeeReceipt[token] == 0;

// For any token, the balance of the contract is always greater than or equal to the sum of all collateral, withdrawable, and claimable settlement fee amounts for that token minus the flash loaned amount.
// Note: this invariant is strong, so it also holds before each external call.
strong invariant tokenBalanceCorrect(address token)
    tokenBalances[token][currentContract] >= collateralSum(token) + withdrawableSum(token) + claimableSettlementFee(token) - flashloans[token] - pendingFeeReceipt[token]
```
