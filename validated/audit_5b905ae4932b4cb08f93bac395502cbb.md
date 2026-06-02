All four code references check out against the actual source. Let me verify the key facts before rendering a verdict:

**[1] Lines 508–509 confirmed:** [1](#0-0) 

**[2] Line 520 confirmed — transfer after accounting:** [2](#0-1) 

**[3] `safeTransferFrom` — only checks code length, call success, boolean return; no balance delta:** [3](#0-2) 

**[4] Fee-on-transfer not excluded:** SECURITY.md contains no such exclusion. `live_context.json` line 233 explicitly states fee-on-transfer tokens "should be tested if not explicitly excluded," and line 389 lists "token charges fee" as a fuzz axis. [4](#0-3) 

**Market creation is permissionless** and `touchMarket` validates only LLTV tiers, collateral sorting, and maturity — no loan token validation: [5](#0-4) 

**`withdraw` transfers exactly `units` out**, so an inflated `withdrawable` directly causes an underfunded transfer: [6](#0-5) 

The core solvency invariant "contract token balances must cover withdrawable assets" is broken: [7](#0-6) 

---

Audit Report

## Title
Fee-on-Transfer Loan Token in `repay` Inflates `withdrawable` Beyond Actual Balance — (File: src/Midnight.sol)

## Summary
In `repay`, `marketState[id].withdrawable` is incremented by the caller-supplied `units` before the token transfer executes. When the loan token deducts a transfer fee, only `units - fee` arrives at the contract, but `withdrawable` is credited the full `units`. Subsequent lender `withdraw` calls drain tokens belonging to other lenders or other markets sharing the same token, causing direct, irreversible loss of principal.

## Finding Description
`repay` (lines 508–520 of `src/Midnight.sol`) performs accounting unconditionally on `units`:

```solidity
position[id][onBehalf].debt      -= UtilsLib.toUint128(units);  // L508
marketState[id].withdrawable     += UtilsLib.toUint128(units);  // L509
...
SafeTransferLib.safeTransferFrom(
    market.loanToken, payer, address(this), units               // L520
);
```

`SafeTransferLib.safeTransferFrom` (lines 24–34 of `src/libraries/SafeTransferLib.sol`) validates only `token.code.length > 0`, call success, and the boolean return value — no balance-before/balance-after measurement is performed. For a fee-on-transfer token, `transferFrom` returns `true` and emits a transfer event for `units`, but only `units - fee` arrives at `address(this)`.

`withdraw` (lines 494–499 of `src/Midnight.sol`) decrements `withdrawable` by `units` and then calls `safeTransfer(market.loanToken, receiver, units)`, transferring the full `units` out. If `withdrawable` was inflated during `repay`, the contract attempts to transfer more tokens than it holds, draining funds from other lenders or markets.

Market creation via `touchMarket` is permissionless and validates only LLTV tiers, collateral sorting, and maturity — no loan token type validation is performed. Any actor can create a market with a fee-on-transfer ERC-20 as `loanToken`.

**Exploit flow:**
1. Attacker creates a market with a fee-on-transfer ERC-20 (e.g., a deflationary token that always charges fees) as `loanToken`.
2. Lender supplies credit; borrower takes a loan of `D` units via normal protocol flow.
3. Borrower calls `repay(market, D, onBehalf, address(0), "")`.
4. Protocol records: `debt[onBehalf] -= D` and `withdrawable += D`.
5. `safeTransferFrom` transfers `D` tokens; only `D * (1 - feeRate)` arrives at `address(this)`.
6. `withdrawable` now exceeds actual balance by `D * feeRate`.
7. Lenders call `withdraw`; the protocol transfers `D` tokens, draining funds belonging to other lenders or other markets sharing the same token.

Each `repay` call widens the deficit by `units * feeRate`. The existing reentrancy ordering (state before transfer) does not address this because the root cause is the discrepancy between recorded and received amounts, not reentrancy.

## Impact Explanation
Every `repay` with a fee-on-transfer loan token permanently inflates `marketState[id].withdrawable` relative to the contract's actual token balance, violating the core solvency invariant ("contract token balances must cover withdrawable assets"). Lenders who withdraw after such repayments receive tokens deposited by other lenders or belonging to other markets, causing direct, irreversible loss of lender principal. This is a direct theft-of-assets impact.

## Likelihood Explanation
Market creation is permissionless with no token validation, so any unprivileged actor (listed in the attacker model as "market creator") can create a market with a fee-on-transfer loan token. The exploit requires only a standard borrow-then-repay flow. It is repeatable: each repayment widens the deficit. Fee-on-transfer tokens are not excluded by `SECURITY.md`, and `live_context.json` explicitly lists "token charges fee" as an external behavior to fuzz and states fee-on-transfer tokens "should be tested if not explicitly excluded."

## Recommendation
Measure the actual received amount using a balance-before/balance-after pattern in `repay`, and credit `withdrawable` only by the delta:

```solidity
uint256 balanceBefore = IERC20(market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
uint256 received = IERC20(market.loanToken).balanceOf(address(this)) - balanceBefore;
position[id][onBehalf].debt      -= UtilsLib.toUint128(received);
marketState[id].withdrawable     += UtilsLib.toUint128(received);
```

Alternatively, explicitly document and enforce that fee-on-transfer tokens are not supported as loan tokens, and add a validation check at market creation time (though this is difficult to enforce on-chain for tokens with owner-togglable fees).

## Proof of Concept
**Minimal Foundry test plan:**

1. Deploy a mock ERC-20 that deducts a 10% fee on every `transferFrom` call.
2. Create a market with this token as `loanToken` via `touchMarket`.
3. Have a lender supply credit and a borrower take a loan of `1000` units.
4. Borrower calls `repay(market, 1000, borrower, address(0), "")`.
5. Assert: `midnight.withdrawable(id) == 1000` (inflated).
6. Assert: `feeToken.balanceOf(address(midnight)) == 900` (only 900 received).
7. Lender calls `withdraw(market, 1000, lender, lender)`.
8. Assert: call succeeds and drains 100 tokens from other depositors, or reverts with insufficient balance if no other funds exist — either outcome demonstrates the accounting desync.

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

**File:** src/Midnight.sol (L520-520)
```text
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

**File:** live_context.json (L187-189)
```json
    "solvency": [
      "contract token balances must cover withdrawable assets, collateral claims, credit redemptions, and accrued fees",
      "total claimable credit must not exceed repaid loan assets plus valid recoverable debt after loss accounting",
```

**File:** live_context.json (L231-234)
```json
      "callbacks must not corrupt partial state through reentrancy",
      "ERC20 transfer deltas must match accounting deltas",
      "fee-on-transfer, rebasing, false-return, ERC777-like hooks, and non-standard decimals should be tested if not explicitly excluded",
      "multicall must not bypass per-action invariants"
```
