Audit Report

## Title
Fee-on-Transfer `loanToken` in `repay` Inflates `withdrawable` Beyond Actual Balance - (File: src/Midnight.sol)

## Summary
The `repay` function credits `marketState[id].withdrawable += units` before executing the token transfer, but `SafeTransferLib.safeTransferFrom` only validates the boolean return of `transferFrom` and cannot detect a fee deduction. When a fee-on-transfer token is used as `loanToken`, the contract records the full `units` as withdrawable while receiving only `units * (1 - fee_rate)`, breaking the invariant `token_balance(address(this)) >= withdrawable`. Because `touchMarket` imposes no restriction on `loanToken` type and market creation is permissionless, this accounting gap is reachable by any unprivileged actor and compounds with every repayment, ultimately freezing the last lenders' funds.

## Finding Description
**Root cause — `repay` (lines 502–521):** [1](#0-0) 

`position[id][onBehalf].debt` is decremented and `marketState[id].withdrawable` is incremented by the full `units` before the transfer executes. [2](#0-1) 

The transfer is the last operation; only `units * (1 - f)` tokens actually arrive.

**`SafeTransferLib.safeTransferFrom` (lines 24–34):** [3](#0-2) 

The library checks only that `transferFrom` returns `true`. It performs no pre/post balance-delta check and cannot observe a fee deduction.

**`touchMarket` (lines 755–791):** [4](#0-3) 

Validation covers maturity, collateral sorting, LLTV tiers, and `maxLif`. There is no restriction on `loanToken`; any ERC20-compatible address, including a fee-on-transfer token, is accepted.

**`withdraw` (lines 481–500):** [5](#0-4) 

`withdraw` decrements `withdrawable` by `units` and transfers exactly `units` tokens. Once the real token balance falls below `withdrawable` due to accumulated fee gaps, the last lenders' `safeTransfer` calls revert (insufficient balance), permanently freezing their credited funds.

**Exploit flow:**
1. Attacker (or any user) calls `touchMarket` with a fee-on-transfer ERC20 as `loanToken` — permissionless, no privilege required.
2. Lenders supply credit; borrowers accumulate debt via normal `take` interactions.
3. Borrower calls `repay(market, N, onBehalf, address(0), '')`.
4. Line 509: `withdrawable += N` — full `N` recorded.
5. Line 520: contract receives only `N * (1 - f)` tokens.
6. Gap = `N * f` tokens per repayment; repeatable and cumulative.
7. Early lenders calling `withdraw` drain the real balance; later lenders' `withdraw` calls revert.

## Impact Explanation
The core accounting invariant `token_balance(address(this)) >= withdrawable` is permanently violated. Lenders who withdraw first are made whole; subsequent lenders find the contract insolvent and their funds are irreversibly frozen. The shortfall equals the cumulative fee deducted across all repayments and grows monotonically with market usage. This constitutes a permanent, partial freeze of user funds — a concrete in-scope impact under the "Permanent lock, freeze, or unrecoverable corruption of user/project state" category in RESEARCHER.md.

## Likelihood Explanation
Market creation is permissionless via `touchMarket`; no privileged key is required. Fee-on-transfer tokens are a well-known, deployed ERC20 variant. The bug is triggered on every `repay` call in such a market, making it repeatable and cumulative. No victim mistake is required — lenders interact with a market that appears legitimate. The only precondition is the existence of a market with a fee-on-transfer `loanToken`, which any external actor can create.

## Recommendation
1. **Post-transfer balance check in `repay`**: Record the contract's token balance before and after `safeTransferFrom`; use the actual delta (not `units`) to increment `withdrawable`.
2. **Alternatively, restrict `loanToken` in `touchMarket`**: Maintain an allowlist of approved loan tokens (enforced by a privileged setter), ensuring only standard ERC20 tokens without transfer fees are accepted.
3. Option 1 is preferable as it is permissionless-compatible and closes the gap without requiring governance overhead.

## Proof of Concept
**Minimal Foundry test plan:**
1. Deploy a mock ERC20 with a 10% transfer fee (deducts 10% from `to` on every `transferFrom`).
2. Call `touchMarket` with this token as `loanToken`.
3. Lender calls `supply`; borrower calls `take` to accumulate debt.
4. Borrower calls `repay(market, 1000, borrower, address(0), '')`.
5. Assert: `marketState[id].withdrawable` increased by 1000, but `IERC20(loanToken).balanceOf(address(midnight))` increased by only 900.
6. Assert: `withdrawable > token_balance` — invariant broken.
7. Lender calls `withdraw` for full credit; assert it succeeds (drains real balance).
8. Second lender calls `withdraw`; assert it reverts with insufficient balance — funds frozen.

### Citations

**File:** src/Midnight.sol (L494-499)
```text
        _marketState.withdrawable -= UtilsLib.toUint128(units);
        _marketState.totalUnits -= UtilsLib.toUint128(units);

        emit EventsLib.Withdraw(msg.sender, id, units, onBehalf, receiver, pendingFeeDecrease);

        SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
```

**File:** src/Midnight.sol (L508-509)
```text
        position[id][onBehalf].debt -= UtilsLib.toUint128(units);
        marketState[id].withdrawable += UtilsLib.toUint128(units);
```

**File:** src/Midnight.sol (L520-521)
```text
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
    }
```

**File:** src/Midnight.sol (L757-773)
```text
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
