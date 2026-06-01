### Title
Fee-on-Transfer loanToken Causes `claimableSettlementFee` Overstatement in `take()` - (File: src/Midnight.sol)

### Summary
In `take()`, `claimableSettlementFee` is incremented at face value (`buyerAssets - sellerAssets`) on line 418 before the actual `safeTransferFrom` on line 455 delivers only `(buyerAssets - sellerAssets) * (1 - fee_rate)` to the protocol. Because market creation is permissionless and the loanToken is arbitrary, any taker operating in a fee-on-transfer loanToken market can repeatedly widen the gap between `claimableSettlementFee` and the protocol's actual token balance, breaking the core solvency invariant.

### Finding Description
**Code path:**

`take()` → line 418 (accounting increment) → line 455 (first `safeTransferFrom` to `address(this)`) → line 456 (second `safeTransferFrom` to `receiver`)

```
// Line 418 — face-value accounting
claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;

// Line 455 — actual transfer to protocol; fee-on-transfer token delivers less
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);

// Line 456 — transfer to seller's receiver; also loses fee_rate, borne by seller
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

`SafeTransferLib.safeTransferFrom` (lines 24–34 of `src/libraries/SafeTransferLib.sol`) only checks the boolean return value; it performs no balance-before/balance-after check. There is no `balanceOf` snapshot anywhere in `take()`.

**Root cause:** The accounting write at line 418 is unconditional and uses the nominal `buyerAssets - sellerAssets` value. For a fee-on-transfer token the protocol actually receives `(buyerAssets - sellerAssets) * (1 - f)`, creating a permanent deficit of `(buyerAssets - sellerAssets) * f` in `claimableSettlementFee` relative to the real balance.

**Attacker-controlled inputs:** `offer.market.loanToken` (set at market creation, which is permissionless per `touchMarket` — no access control, no token whitelist), `units`, `offer.tick`. The taker controls `units` and can choose any existing fee-on-transfer loanToken market.

**Why existing checks fail:** `touchMarket` validates only maturity, collateral count, LLTV, and LIF — no token type restriction. `SafeTransferLib` validates only the ERC-20 boolean return. No post-transfer balance check exists anywhere in `take()`.

**Second transfer (line 456):** Goes directly from `payer` to `receiver` (seller), bypassing the protocol's balance. The seller's credit is reduced by `sellerCreditDecrease` at face value (line 413) while they receive only `sellerAssets * (1 - f)`. This is a simultaneous loss to the seller but does not directly widen the protocol's `claimableSettlementFee` deficit further — the deficit is driven solely by the first transfer.

### Impact Explanation
After each `take()` on a fee-on-transfer loanToken market:

```
claimableSettlementFee[token] += (buyerAssets - sellerAssets)          // accounting
balanceOf(midnight, token)    += (buyerAssets - sellerAssets) * (1-f)  // reality
```

The invariant `loanToken.balanceOf(midnight) >= withdrawable + claimableSettlementFee + continuousFeeCredit` is violated by `(buyerAssets - sellerAssets) * f` per call. Repeated takes compound the deficit linearly. When `feeClaimer` calls `claimSettlementFee`, the transfer will revert or drain tokens that belong to lenders' `withdrawable` pool, causing insolvency for lenders.

### Likelihood Explanation
**Preconditions:**
1. A market exists (or is created) with a fee-on-transfer token as `loanToken` — permissionless, no admin action required.
2. A maker has published a buy or sell offer in that market — normal protocol usage.
3. A taker calls `take()` — no privilege required.

The scenario is fully reachable by unprivileged actors. It is repeatable on every `take()` call in the affected market. The deficit grows monotonically and cannot be corrected without an out-of-band token donation.

### Recommendation
Replace the face-value accounting increment with an actual-received-amount pattern using a balance snapshot:

```solidity
uint256 balanceBefore = IERC20(offer.market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
uint256 actualReceived = IERC20(offer.market.loanToken).balanceOf(address(this)) - balanceBefore;
claimableSettlementFee[offer.market.loanToken] += actualReceived;
```

Alternatively, explicitly disallow fee-on-transfer tokens by asserting `actualReceived == buyerAssets - sellerAssets` and reverting otherwise. The same pattern should be applied to `repay()` (line 520) and `supplyCollateral()` (line 545) for consistency.

### Proof of Concept
```solidity
// Foundry fuzz test
function testFuzz_FeeOnTransferTakeBreaksClaimableSettlementFee(
    uint256 units,
    uint16 feeBps  // token transfer fee in basis points, e.g. 100 = 1%
) public {
    // Preconditions
    vm.assume(units > 1e6 && units < 1e24);
    vm.assume(feeBps > 0 && feeBps < 1000); // 0.01% – 10%

    // Deploy fee-on-transfer loanToken with feeBps
    FeeOnTransferToken feeToken = new FeeOnTransferToken(feeBps);
    // Create market with feeToken as loanToken (permissionless)
    Market memory feeMarket = buildValidMarket(address(feeToken));
    bytes32 fid = midnight.touchMarket(feeMarket);

    // Setup: maker publishes buy offer, taker has collateral
    // ... (standard test setup) ...

    uint256 claimableBefore = midnight.claimableSettlementFee(address(feeToken));
    uint256 balanceBefore   = feeToken.balanceOf(address(midnight));

    // Taker executes take()
    vm.prank(taker);
    (uint256 buyerAssets, uint256 sellerAssets) = midnight.take(offer, "", units, taker, taker, address(0), "");

    uint256 claimableAfter = midnight.claimableSettlementFee(address(feeToken));
    uint256 balanceAfter   = feeToken.balanceOf(address(midnight));

    uint256 accountingDelta = claimableAfter - claimableBefore;   // face value
    uint256 actualReceived  = balanceAfter  - balanceBefore;      // real tokens

    // Core assertion: protocol received less than it accounted for
    assertLt(actualReceived, accountingDelta, "fee-on-transfer deficit");

    // Invariant assertion: balance must cover claimable + withdrawable + continuousFeeCredit
    (,, uint128 withdrawable, uint128 continuousFeeCredit,,,,,,,,, ) = midnight.marketState(fid);
    assertGe(
        feeToken.balanceOf(address(midnight)),
        uint256(withdrawable) + claimableAfter + uint256(continuousFeeCredit),
        "solvency invariant violated"  // this assertion FAILS
    );
}
```

**Expected result:** `actualReceived < accountingDelta` passes; the solvency `assertGe` **fails**, confirming the invariant is broken. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/Midnight.sol (L408-418)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);

        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
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
