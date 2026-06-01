### Title
Fee-on-transfer loan token inflates `_marketState.withdrawable` beyond actual contract balance in `liquidate` - (File: src/Midnight.sol)

### Summary

In `liquidate`, `_marketState.withdrawable` is incremented by the full `repaidUnits` before `SafeTransferLib.safeTransferFrom` is called to pull those units from the payer. `SafeTransferLib.safeTransferFrom` only verifies the call succeeded and returned `true`; it does not compare pre/post balances. When the loan token silently deducts a transfer fee, the contract receives `repaidUnits - fee` but records `repaidUnits` as withdrawable, permanently inflating the accounting. Lenders who subsequently call `withdraw` will drain the contract faster than tokens arrive, causing the final lender(s) to receive a revert or shortfall.

### Finding Description

**Exact code path:**

`src/Midnight.sol`, `liquidate()`:

```
// Line 675 — accounting updated with full repaidUnits
_marketState.withdrawable += UtilsLib.toUint128(repaidUnits);
_position.debt -= UtilsLib.toUint128(repaidUnits);

// ... collateral transfer, optional callback ...

// Line 717 — only (repaidUnits - fee) actually arrives
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
```

`src/libraries/SafeTransferLib.sol`, `safeTransferFrom()` (lines 24–34): checks only `success` and the boolean return value. No balance snapshot is taken before or after the call, so a fee silently retained by the token contract is invisible to the protocol.

**Market creation:** `touchMarket()` (lines 755–791) validates collateral params (lltv tiers, maxLif, sort order) but imposes **no restriction on the loan token type**. Any ERC20, including a fee-on-transfer token, is accepted.

**Attacker-controlled inputs:**
- `market.loanToken` — set at market creation time (permissionless via `touchMarket`)
- `repaidUnits` — chosen by the liquidator; any value `> 0` triggers the discrepancy

**Exploit flow:**
1. Deploy (or use) a fee-on-transfer ERC20 token `FeeToken` with fee `f` per transfer.
2. Call `touchMarket(market)` with `market.loanToken = address(FeeToken)` — succeeds with no restriction.
3. Lenders supply credit; borrowers take debt denominated in `FeeToken`.
4. Borrower becomes liquidatable (price drop or post-maturity).
5. Liquidator calls `liquidate(..., repaidUnits=R, ...)`.
6. Line 675 executes: `_marketState.withdrawable += R`.
7. Line 717 executes: `FeeToken.transferFrom(payer, address(this), R)` — contract receives `R - f`.
8. Post-call invariant broken: `IERC20(FeeToken).balanceOf(address(this)) < _marketState.withdrawable` by exactly `f`.
9. Each subsequent liquidation or repay compounds the deficit.
10. When lenders call `withdraw()` (line 499: `SafeTransferLib.safeTransfer(loanToken, receiver, units)`), the last lender(s) to withdraw will trigger a revert because the contract holds fewer tokens than `withdrawable` promises.

**Why existing checks fail:**
- `SafeTransferLib.safeTransferFrom` has no balance-delta check. [1](#0-0) 
- `touchMarket` has no loan token allowlist or fee-on-transfer guard. [2](#0-1) 
- The Certora `Solvency.spec` explicitly assumes "no fee taking from sender or receiver" in its ERC20 summary, meaning formal verification does not cover this case. [3](#0-2) 
- The `live_context.json` core invariant "ERC20 transfer deltas must match accounting deltas" is violated, and the same file notes fee-on-transfer tokens "should be tested if not explicitly excluded" — they are not excluded anywhere in the code. [4](#0-3) 

The identical pattern exists in `repay()` (lines 509 and 520), compounding the deficit further. [5](#0-4) 

### Impact Explanation

Every `liquidate` call with `repaidUnits > 0` against a fee-on-transfer loan token inflates `_marketState.withdrawable` by the fee amount without a corresponding token balance increase. [6](#0-5)  `withdraw()` transfers `units` of the loan token directly to the receiver using the inflated `withdrawable` as the accounting source. [7](#0-6)  Once cumulative fees exceed the contract's actual balance, `safeTransfer` reverts for the last withdrawing lender(s), permanently locking their credit. This is protocol insolvency: lenders collectively hold more withdrawable credit than the contract can honour.

### Likelihood Explanation

**Preconditions:**
1. A market must exist with a fee-on-transfer loan token — permissionless, any address can call `touchMarket`.
2. A borrower must be liquidatable — normal market operation.
3. A liquidator calls `liquidate` with `repaidUnits > 0` — standard liquidation flow.

All three preconditions are reachable by unprivileged actors without any admin action. The bug is triggered on every single liquidation in such a market, making it repeatable and cumulative. Real-world fee-on-transfer tokens (e.g., tokens with built-in tax mechanisms) are deployed on mainnet and could be used as loan tokens.

### Recommendation

Record the contract's loan token balance before and after the `safeTransferFrom` call, and use the actual received delta (not `repaidUnits`) to update `_marketState.withdrawable`. Apply the same fix to `repay()`. For example, in `liquidate`:

```solidity
uint256 balanceBefore = IERC20(market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
uint256 received = IERC20(market.loanToken).balanceOf(address(this)) - balanceBefore;
// Use `received` instead of `repaidUnits` for withdrawable accounting,
// or revert if received != repaidUnits to explicitly reject fee-on-transfer tokens.
```

Alternatively, add an explicit check in `touchMarket` that rejects loan tokens whose `transferFrom` delivers less than the requested amount (a one-time probe transfer of 1 wei).

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";

// Fee-on-transfer ERC20: deducts 10% on every transferFrom
contract FeeToken is ERC20 {
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount / 10;
        super.transferFrom(from, to, amount - fee); // only amount-fee arrives at `to`
        _burn(from, fee);                           // fee is destroyed (or sent to fee collector)
        return true;
    }
}

contract FeeOnTransferLiquidateTest is Test {
    Midnight midnight;
    FeeToken loanToken;
    // ... standard test setup: collateral token, oracle, market, lender, borrower ...

    function testFeeOnTransferInflatesWithdrawable() public {
        // 1. Setup: create market with FeeToken as loan token
        // 2. Lender supplies credit (take), borrower takes debt
        // 3. Drop oracle price to make borrower liquidatable
        // 4. Liquidator calls liquidate with repaidUnits = R

        uint256 withdrawableBefore = midnight.withdrawable(id);
        uint256 balanceBefore = loanToken.balanceOf(address(midnight));

        midnight.liquidate(market, 0, 0, R, borrower, false, address(this), address(0), "");

        uint256 withdrawableAfter = midnight.withdrawable(id);
        uint256 balanceAfter = loanToken.balanceOf(address(midnight));

        // KEY ASSERTIONS:
        // withdrawable increased by full R
        assertEq(withdrawableAfter, withdrawableBefore + R, "withdrawable inflated by full R");
        // but contract only received R - fee (R * 0.9)
        assertEq(balanceAfter - balanceBefore, R * 9 / 10, "contract received only R - fee");
        // INVARIANT VIOLATION: balance < withdrawable
        assertLt(balanceAfter, withdrawableAfter, "INVARIANT BROKEN: balance < withdrawable");

        // 5. Demonstrate insolvency: lenders withdraw until contract is drained
        // The last lender's withdraw() reverts because safeTransfer cannot cover the full amount
        vm.expectRevert();
        midnight.withdraw(market, withdrawableAfter, lender, lender);
    }
}
```

**Expected assertions:**
- `balanceAfter - balanceBefore < R` (fee-on-transfer delivered less)
- `withdrawableAfter == withdrawableBefore + R` (accounting uses full R)
- `balanceAfter < withdrawableAfter` (invariant broken)
- Final `withdraw` for the full `withdrawable` amount reverts (insolvency)

### Citations

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

**File:** src/Midnight.sol (L675-677)
```text
            _marketState.withdrawable += UtilsLib.toUint128(repaidUnits);
            _position.debt -= UtilsLib.toUint128(repaidUnits);
        }
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

**File:** certora/specs/Solvency.spec (L31-34)
```text
    // Assume ERC20 tokens transfer correctly: no fee taking from sender or receiver, no rebasing, no blacklisting, no transfer limits.
    function _.transfer(address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, e.msg.sender, a, v) expect(bool);
    function _.transferFrom(address src, address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, src, a, v) expect(bool);
}
```

**File:** live_context.json (L231-234)
```json
      "callbacks must not corrupt partial state through reentrancy",
      "ERC20 transfer deltas must match accounting deltas",
      "fee-on-transfer, rebasing, false-return, ERC777-like hooks, and non-standard decimals should be tested if not explicitly excluded",
      "multicall must not bypass per-action invariants"
```
