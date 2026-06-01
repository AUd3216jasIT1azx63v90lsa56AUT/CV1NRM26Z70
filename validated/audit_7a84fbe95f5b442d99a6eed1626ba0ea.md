Audit Report

## Title
Fee-on-Transfer `loanToken` in `repay()` Inflates `withdrawable` Beyond Actual Balance - (File: src/Midnight.sol)

## Summary
`repay()` unconditionally increments `marketState[id].withdrawable` by `units` before transferring tokens, while `SafeTransferLib.safeTransferFrom` only validates the boolean return value of `transferFrom` — not the actual tokens received. When a fee-on-transfer token is used as `loanToken`, the contract receives fewer tokens than credited, permanently breaking the solvency invariant `withdrawable ≤ loanToken.balanceOf(address(this))`. Because market creation is permissionless and no restriction on `loanToken` type exists, any unprivileged user can trigger this condition.

## Finding Description
**Root cause:** `repay()` at [1](#0-0)  credits `withdrawable` with the full `units` value before the transfer executes. The subsequent transfer at [2](#0-1)  uses `SafeTransferLib.safeTransferFrom`, which only checks the boolean return value of `transferFrom` and does not compare pre/post balances. [3](#0-2) 

**No loanToken restriction:** `touchMarket()` validates collateral params, LLTV, maxLif, and maturity, but imposes no restriction on `loanToken` type — any ERC20 is accepted. [4](#0-3) 

**Exploit flow:**
1. Attacker calls `touchMarket()` with a fee-on-transfer token (e.g., 1% fee) as `loanToken`. Market creation is fully permissionless.
2. Lender supplies liquidity; borrower takes debt of `1000e18 units`.
3. Borrower calls `repay(market, 1000e18, borrower, address(0), "")`.
4. Line 508: `position[id][borrower].debt` decremented by `1000e18`.
5. Line 509: `marketState[id].withdrawable` incremented by `1000e18`.
6. Line 520: `safeTransferFrom` requests `1000e18`; token deducts 1% fee; contract receives only `990e18`.
7. `withdrawable` is now `1000e18` but actual balance increased by only `990e18` — a `10e18` deficit per repayment, cumulative and unbounded.

**Same pattern in `liquidate()` and `take()`:** [5](#0-4)  and [6](#0-5)  both perform inbound transfers without balance snapshot checks, compounding the deficit across all inbound transfer paths.

## Impact Explanation
After each fee-on-transfer repayment, `marketState[id].withdrawable` exceeds the actual `loanToken` balance held by the contract. `withdraw()` at [7](#0-6)  sends exactly `units` of `loanToken` per credit unit redeemed, drawing from the real balance. The first lenders to withdraw drain the real balance; subsequent lenders' `withdraw()` calls revert (insufficient balance), or — if the contract holds tokens from other markets sharing the same `loanToken` — those other markets' funds are silently consumed. This constitutes direct protocol insolvency: lender credit claims are unbacked.

## Likelihood Explanation
Market creation is fully permissionless — any address can call `touchMarket()` with any `loanToken`. Fee-on-transfer tokens (e.g., USDT with fee enabled, STA, PAXG, or any custom ERC20) are real and deployable. The borrower calling `repay()` needs no special privilege beyond `onBehalf == msg.sender` or authorization. The bug is triggered on every single `repay()` call in such a market, making it repeatable and cumulative. No precondition requires a privileged actor.

## Recommendation
Use a balance-before/balance-after pattern in all inbound transfer paths (`repay`, `liquidate`, `take`) to determine the actual amount received, and credit `withdrawable` only with the delta:

```solidity
uint256 balanceBefore = IERC20(market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
uint256 received = IERC20(market.loanToken).balanceOf(address(this)) - balanceBefore;
marketState[id].withdrawable += UtilsLib.toUint128(received);
```

Alternatively, explicitly document and enforce (via `touchMarket()`) that fee-on-transfer tokens are not supported as `loanToken`, reverting on market creation if the token exhibits transfer fees (though this is harder to enforce on-chain without a transfer test).

## Proof of Concept
1. Deploy a mock ERC20 with a 1% transfer fee.
2. Call `touchMarket()` with this token as `loanToken`.
3. Supply `1000e18` units as a lender.
4. Borrow `1000e18` units as a borrower.
5. Call `repay(market, 1000e18, borrower, address(0), "")`.
6. Assert: `marketState[id].withdrawable == 1000e18` but `loanToken.balanceOf(address(Midnight)) == 990e18`.
7. Attempt lender `withdraw()` for `1000e18` — call reverts due to insufficient balance, confirming insolvency.

### Citations

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/Midnight.sol (L499-499)
```text
        SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
```

**File:** src/Midnight.sol (L508-509)
```text
        position[id][onBehalf].debt -= UtilsLib.toUint128(units);
        marketState[id].withdrawable += UtilsLib.toUint128(units);
```

**File:** src/Midnight.sol (L520-520)
```text
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
```

**File:** src/Midnight.sol (L717-717)
```text
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
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
