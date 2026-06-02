Audit Report

## Title
Fee-on-Transfer Loan Token Causes Accounting Discrepancy and Receiver Shortfall in `withdraw` and `repay` - (File: src/Midnight.sol)

## Summary
When a market's `loanToken` is a fee-on-transfer token, `repay` credits `marketState[id].withdrawable` with the full `units` value while the contract only receives `units*(1-f)` actual tokens. Subsequently, `withdraw` deducts `units` from accounting and calls `safeTransfer(market.loanToken, receiver, units)`, which succeeds (returning `true`) but delivers only `units*(1-f)` to the receiver. The `units*f` shortfall per cycle accumulates, eventually making other lenders unable to withdraw their full credit.

## Finding Description

**Repay introduces the primary discrepancy** (`src/Midnight.sol:508-520`):
```solidity
position[id][onBehalf].debt      -= UtilsLib.toUint128(units);  // line 508
marketState[id].withdrawable     += UtilsLib.toUint128(units);  // line 509
// ...
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units); // line 520
```
With a fee-on-transfer token, `safeTransferFrom` succeeds and returns `true`, but the contract receives only `units*(1-f)`. Accounting records `+units` to `withdrawable`.

**Withdraw compounds it** (`src/Midnight.sol:493-499`):
```solidity
_position.credit          -= UtilsLib.toUint128(units);   // line 493
_marketState.withdrawable -= UtilsLib.toUint128(units);   // line 494
_marketState.totalUnits   -= UtilsLib.toUint128(units);   // line 495
SafeTransferLib.safeTransfer(market.loanToken, receiver, units); // line 499
```
`safeTransfer` calls `token.transfer(receiver, units)`, which returns `true` but delivers only `units*(1-f)` to the receiver. The contract's actual balance decreases by `units` (or the transfer reverts if the contract's balance is already depleted to `units*(1-f)` from the repay shortfall).

**`SafeTransferLib` performs no balance delta check** (`src/libraries/SafeTransferLib.sol:15-21`):
```solidity
(bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transfer, (to, value)));
// only checks: success == true, returndata == true or empty
// NO check: receiver.balanceOf(after) - receiver.balanceOf(before) == value
```

**`touchMarket` imposes no restriction on `loanToken` type** (`src/Midnight.sol:755-791`): validation covers only collateral params, LLTV, maxLif, and maturity. Any ERC20 address is accepted.

**`live_context.json` line 233** explicitly flags: *"fee-on-transfer, rebasing, false-return, ERC777-like hooks, and non-standard decimals should be tested if not explicitly excluded"* — no explicit exclusion exists in the core contract or `SECURITY.md`.

## Impact Explanation
Per repay-withdraw cycle, `units*f` tokens are irrecoverably lost to the fee mechanism. The contract's actual token balance falls below what `marketState[id].withdrawable` records as redeemable. This violates the core solvency invariant that contract balances must cover all withdrawable credit. Repeated cycles drain the pool, causing later lenders to receive less than their credited amount or have their `withdraw` calls revert entirely when the contract's balance is exhausted. This constitutes direct, permanent loss of lender funds and potential insolvency of the affected market.

## Likelihood Explanation
Market creation is fully permissionless — any address can call `touchMarket` with any `loanToken`. Fee-on-transfer tokens exist on mainnet (deflationary tokens, tokens with protocol fee mechanics). No special privilege is required: any lender in a market using such a token triggers the discrepancy through normal `repay` and `withdraw` flows. The condition is repeatable on every cycle and self-compounding.

## Recommendation
1. **Measure actual received amount in `repay`**: record `balanceBefore = IERC20(market.loanToken).balanceOf(address(this))` before `safeTransferFrom`, then compute `received = balanceAfter - balanceBefore`, and credit `withdrawable += received` instead of `units`.
2. **Measure actual sent amount in `withdraw`**: similarly guard `safeTransfer` with a balance check and revert or adjust if the delta does not equal `units`.
3. **Alternatively**, explicitly document and enforce that fee-on-transfer tokens are not supported as `loanToken`, and add a check or note in `touchMarket` to that effect.

## Proof of Concept
1. Deploy a mock ERC20 with a 5% transfer fee (fee deducted from transferred amount, returns `true`).
2. Create a Midnight market with this token as `loanToken`.
3. Lender deposits; borrower takes and receives loan tokens.
4. Borrower calls `repay(market, 100e18, borrower, address(0), "")`:
   - Contract receives 95e18 tokens; `withdrawable += 100e18`.
5. Lender calls `withdraw(market, 100e18, lender, receiver)`:
   - If contract has ≥100e18 balance (from other lenders): transfer succeeds, receiver gets 95e18, contract balance drops by 100e18. Net: 5e18 lost per cycle, other lenders shortchanged.
   - If contract has exactly 95e18: `transfer(receiver, 100e18)` reverts (insufficient balance), lender cannot withdraw at all.
6. Assert `IERC20(loanToken).balanceOf(receiver) == 95e18` while accounting recorded `100e18` withdrawn — discrepancy confirmed. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/Midnight.sol (L493-499)
```text
        _position.credit -= UtilsLib.toUint128(units);
        _marketState.withdrawable -= UtilsLib.toUint128(units);
        _marketState.totalUnits -= UtilsLib.toUint128(units);

        emit EventsLib.Withdraw(msg.sender, id, units, onBehalf, receiver, pendingFeeDecrease);

        SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
```

**File:** src/Midnight.sol (L508-520)
```text
        position[id][onBehalf].debt -= UtilsLib.toUint128(units);
        marketState[id].withdrawable += UtilsLib.toUint128(units);

        address payer = callback != address(0) ? callback : msg.sender;
        emit EventsLib.Repay(msg.sender, id, units, onBehalf, payer);

        if (callback != address(0)) {
            require(
                IRepayCallback(callback).onRepay(id, market, units, onBehalf, data) == CALLBACK_SUCCESS,
                WrongRepayCallbackReturnValue()
            );
        }
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
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

**File:** src/libraries/SafeTransferLib.sol (L12-22)
```text
    function safeTransfer(address token, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());

        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transfer, (to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
        }
        require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferReturnedFalse());
    }
```

**File:** live_context.json (L230-235)
```json
    "external_calls": [
      "callbacks must not corrupt partial state through reentrancy",
      "ERC20 transfer deltas must match accounting deltas",
      "fee-on-transfer, rebasing, false-return, ERC777-like hooks, and non-standard decimals should be tested if not explicitly excluded",
      "multicall must not bypass per-action invariants"
    ]
```
