### Title
Settlement Fee Overcounted When Fee-on-Transfer loanToken Used With buyerCallback Payer - (File: src/Midnight.sol)

### Summary

In `take()`, `claimableSettlementFee[offer.market.loanToken]` is incremented by the full `buyerAssets - sellerAssets` on line 418 **before** the inbound transfer on line 455. When `loanToken` is a fee-on-transfer token and `payer` is `buyerCallback` (line 422), `safeTransferFrom` delivers fewer tokens to the protocol than the recorded amount, permanently overcounting `claimableSettlementFee` relative to the actual contract balance. Market creation is permissionless with an arbitrary `loanToken`, making this reachable by an unprivileged maker.

### Finding Description

**Code path:**

1. `take()` computes `buyerAssets - sellerAssets` as the settlement fee spread.
2. **Line 418**: `claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets` — state written before any transfer.
3. **Line 422**: `address payer = buyerCallback != address(0) ? buyerCallback : ...` — when `offer.buy == true` and `offer.callback != address(0)`, `payer` is set to `buyerCallback`.
4. **Lines 445–452**: `IBuyCallback(buyerCallback).onBuy(...)` is called and must return `CALLBACK_SUCCESS`.
5. **Line 455**: `SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets)` — if `loanToken` is fee-on-transfer, the contract receives `(buyerAssets - sellerAssets) * (1 - fee_rate)`, not the full amount.

**Root cause:** No balance-before/after check surrounds line 455. `SafeTransferLib.safeTransferFrom` (Solady) does not verify the received amount. The Certora `Solvency.spec` explicitly axiomatizes away this class of token at line 31: *"Assume ERC20 tokens transfer correctly: no fee taking from sender or receiver."* The `pendingFeeReceiptZero` weak invariant and `tokenBalanceCorrect` strong invariant are therefore not proven for fee-on-transfer tokens.

**Attacker inputs:**
- Attacker is the **maker** (buyer) in a buy offer (`offer.buy = true`).
- `offer.market.loanToken` = attacker-deployed fee-on-transfer ERC20 (e.g., 1% fee per transfer).
- `offer.callback` = attacker-controlled `IBuyCallback` contract that holds the fee-on-transfer tokens, approves the protocol, and returns `CALLBACK_SUCCESS`.
- Market creation is permissionless (`touchMarket` is `public`, no access control, arbitrary `loanToken` accepted per `live_context.json`).

**Exploit flow:**
1. Attacker deploys fee-on-transfer `loanToken` (1% fee).
2. Attacker calls `touchMarket` to create a market with this `loanToken`.
3. Attacker publishes a buy offer with `offer.callback = attackerCallbackContract`.
4. Any taker calls `take()`:
   - Line 418: `claimableSettlementFee[loanToken] += F` (e.g., F = 100).
   - Line 455: contract receives only `F * 0.99 = 99` tokens.
5. After the call: `claimableSettlementFee[loanToken]` = 100, but actual balance surplus = 99. Shortfall = 1 per take, accumulates across repeated takes.

**Why existing checks fail:** There is no post-transfer balance assertion. The Certora invariants are proven only under the standard-ERC20 assumption. `claimSettlementFee` (line 305–310) calls `safeTransfer(token, receiver, amount)` using the overcounted `claimableSettlementFee` value; if the shortfall is large enough, this either reverts (DoS for fee claimer) or drains tokens belonging to lenders/borrowers from the shared contract balance. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

### Impact Explanation

`claimableSettlementFee[loanToken]` exceeds the actual token surplus held by the contract. Repeated takes accumulate the shortfall. When `feeClaimer` calls `claimSettlementFee` for the full recorded amount, `safeTransfer` either reverts (permanent DoS of fee collection) or, if other token inflows exist (e.g., lender deposits), silently drains funds belonging to lenders or borrowers, violating the core solvency invariant `balance >= collateral + withdrawable + claimableSettlementFee`. [7](#0-6) 

### Likelihood Explanation

**Preconditions:**
- Attacker must deploy or use an existing fee-on-transfer `loanToken` (not a malicious post-deployment action — fee is baked in at construction).
- Attacker must be a maker with a buy offer in that market; market creation is permissionless.
- A taker must fill the offer (attacker can self-take via an authorized operator, since `isAuthorized` allows delegation, though `offer.maker != taker` is enforced — attacker needs a separate taker address).

**Feasibility:** Moderate. Requires a fee-on-transfer token as `loanToken`. Deflationary/tax tokens are a known ERC20 variant. The protocol explicitly supports "arbitrary loan token" with permissionless market creation, so no privileged action is needed. Repeatable across every `take()` call in the affected market. [8](#0-7) 

### Recommendation

After line 455, verify the actual received amount using a balance-before/balance-after check:

```solidity
uint256 balanceBefore = IERC20(offer.market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
uint256 received = IERC20(offer.market.loanToken).balanceOf(address(this)) - balanceBefore;
require(received == buyerAssets - sellerAssets, FeeOnTransferNotSupported());
```

Alternatively, document that fee-on-transfer tokens are unsupported as `loanToken` and add a check or registry to enforce this at market creation time in `touchMarket`. [9](#0-8) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import "../src/Midnight.sol";

// Fee-on-transfer token: 1% fee on every transferFrom
contract FeeToken is ERC20 {
    function transferFrom(address src, address dst, uint256 amt) public override returns (bool) {
        uint256 fee = amt / 100;
        super.transferFrom(src, dst, amt - fee); // dst receives amt - fee
        // fee stays with src (burned or kept)
        return true;
    }
}

// IBuyCallback that holds FeeTokens and approves Midnight
contract AttackerCallback is IBuyCallback {
    function onBuy(...) external returns (bytes4) {
        // approve Midnight to pull buyerAssets from this contract
        FeeToken(token).approve(midnight, type(uint256).max);
        return CALLBACK_SUCCESS;
    }
}

contract FeeOnTransferPoC is Test {
    function testSettlementFeeOvercount() public {
        // 1. Deploy FeeToken, create market with it as loanToken
        // 2. Maker creates buy offer with callback = AttackerCallback
        // 3. Taker calls take()
        uint256 feeBefore = midnight.claimableSettlementFee(address(feeToken));
        uint256 balanceBefore = feeToken.balanceOf(address(midnight));
        
        take(units, taker, makerBuyOffer); // offer.callback = attackerCallback
        
        uint256 feeAfter = midnight.claimableSettlementFee(address(feeToken));
        uint256 balanceAfter = feeToken.balanceOf(address(midnight));
        
        uint256 recordedFee = feeAfter - feeBefore;       // = buyerAssets - sellerAssets (full)
        uint256 actualReceived = balanceAfter - balanceBefore; // < buyerAssets - sellerAssets (after 1% fee)
        
        // ASSERTION: recorded fee exceeds actual received tokens
        assertGt(recordedFee, actualReceived, "claimableSettlementFee overcounted");
        // ASSERTION: invariant violated
        assertGt(
            midnight.claimableSettlementFee(address(feeToken)),
            feeToken.balanceOf(address(midnight)),
            "balance does not cover claimable fee"
        );
    }
}
```

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

**File:** src/Midnight.sol (L422-422)
```text
        address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
```

**File:** src/Midnight.sol (L454-456)
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

**File:** certora/specs/Solvency.spec (L31-33)
```text
    // Assume ERC20 tokens transfer correctly: no fee taking from sender or receiver, no rebasing, no blacklisting, no transfer limits.
    function _.transfer(address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, e.msg.sender, a, v) expect(bool);
    function _.transferFrom(address src, address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, src, a, v) expect(bool);
```

**File:** certora/specs/Solvency.spec (L157-158)
```text
weak invariant pendingFeeReceiptZero(address token)
    pendingFeeReceipt[token] == 0;
```

**File:** certora/specs/Solvency.spec (L160-163)
```text
// For any token, the balance of the contract is always greater than or equal to the sum of all collateral, withdrawable, and claimable settlement fee amounts for that token minus the flash loaned amount.
// Note: this invariant is strong, so it also holds before each external call.
strong invariant tokenBalanceCorrect(address token)
    tokenBalances[token][currentContract] >= collateralSum(token) + withdrawableSum(token) + claimableSettlementFee(token) - flashloans[token] - pendingFeeReceipt[token]
```
