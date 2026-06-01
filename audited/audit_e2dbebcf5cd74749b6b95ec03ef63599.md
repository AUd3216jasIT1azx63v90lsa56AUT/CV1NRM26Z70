### Title
Repay Eliminates Debt Without Token Receipt When loanToken Returns True on No-Op transferFrom - (File: src/Midnight.sol)

### Summary
The `repay` function in `src/Midnight.sol` commits both `position[id][onBehalf].debt -= units` and `marketState[id].withdrawable += units` before calling `SafeTransferLib.safeTransferFrom`. `SafeTransferLib.safeTransferFrom` only validates the boolean return value of `transferFrom`; it performs no before/after balance check. If `market.loanToken.transferFrom` returns `true` without moving tokens, the state mutation is permanently committed with zero token receipt, breaking the invariant that contract balances cover withdrawable assets.

### Finding Description
**Exact code path:**

`repay` (`src/Midnight.sol` lines 502–521): [1](#0-0) 

```
position[id][onBehalf].debt     -= units;   // L508 — state written
marketState[id].withdrawable    += units;   // L509 — state written
...
SafeTransferLib.safeTransferFrom(           // L520 — transfer last
    market.loanToken, payer, address(this), units);
```

`SafeTransferLib.safeTransferFrom` (`src/libraries/SafeTransferLib.sol` lines 24–34): [2](#0-1) 

The library checks:
1. `token.code.length > 0` — passes for any deployed contract.
2. The low-level call does not revert — passes if `transferFrom` returns without reverting.
3. `returndata.length == 0 || abi.decode(returndata, (bool))` — passes if the decoded bool is `true`.

There is **no balance snapshot before the call and no balance verification after it**. A token whose `transferFrom` emits no transfer event, moves no tokens, but returns `true` satisfies all three checks.

**Market creation is fully permissionless and accepts an arbitrary `loanToken`:** [3](#0-2) 

`touchMarket` validates collateral params, LLTV tiers, and maturity, but performs **zero validation on `market.loanToken`** — no code-size check, no interface check, no allowlist. The `live_context.json` explicitly states `"arbitrary loan token"` and `"permissionless market creation"`.

**Attacker-controlled inputs and exploit flow:**

1. Attacker deploys `FakeLoanToken` — a contract with `code.length > 0` whose `transferFrom` always returns `true` and moves nothing.
2. Attacker (as market creator) calls `touchMarket` with `loanToken = address(FakeLoanToken)` and valid collateral/LLTV params. Market is created.
3. A lender calls `take` on a buy-side offer in this market; because `safeTransferFrom` on `FakeLoanToken` also passes, the lender's credit is recorded but no real tokens enter the contract.
4. The borrower (attacker) now holds `position[id][borrower].debt = D` units.
5. Attacker calls `repay(market, D, borrower, address(0), "")`:
   - L505: authorization check passes (`onBehalf == msg.sender`).
   - L508: `position[id][borrower].debt` decremented to 0.
   - L509: `marketState[id].withdrawable` incremented by D.
   - L520: `safeTransferFrom(FakeLoanToken, attacker, address(this), D)` — returns `true`, no tokens move.
6. Debt is zero; `withdrawable` is D; Midnight's actual `FakeLoanToken` balance is unchanged (still 0).

**Why existing checks fail:**

`safeTransferFrom` is the sole guard on token receipt. It is a return-value check, not a balance check. No other function in `repay` inspects `IERC20(loanToken).balanceOf(address(this))` before or after the call. [4](#0-3) 

### Impact Explanation
The borrower's debt is permanently zeroed without any token payment. `marketState[id].withdrawable` is inflated by D units with no backing. Any lender who subsequently calls `withdraw` against that market will attempt to receive tokens that were never deposited, either reverting (fund freeze) or draining tokens deposited by other legitimate repayments in the same market (direct loss to other lenders). The core invariant — *contract balances cover withdrawable assets* — is violated.

### Likelihood Explanation
Preconditions: (a) a `loanToken` that returns `true` on `transferFrom` without moving tokens, and (b) permissionless market creation (confirmed). Condition (a) is reachable by the attacker themselves as market creator deploying a custom token, or by targeting any real token that silently no-ops transfers under certain conditions (paused state, blacklist, etc.) while still returning `true`. The call requires no privilege beyond being the borrower (`onBehalf == msg.sender`). The attack is repeatable across any number of markets and any debt size up to `type(uint128).max`.

### Recommendation
Add a balance check around the `safeTransferFrom` call in `repay` (and symmetrically in `liquidate` at line 717):

```solidity
uint256 balanceBefore = IERC20(market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
require(
    IERC20(market.loanToken).balanceOf(address(this)) >= balanceBefore + units,
    InsufficientTokensReceived()
);
```

This also covers fee-on-transfer tokens. Alternatively, restrict `loanToken` to an allowlist of audited tokens at market creation time in `touchMarket`.

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight, Market, CollateralParams} from "src/Midnight.sol";

contract NoOpToken {
    // Returns true, transfers nothing
    function transferFrom(address, address, uint256) external pure returns (bool) { return true; }
    function transfer(address, uint256) external pure returns (bool) { return true; }
    function balanceOf(address) external pure returns (uint256) { return 0; }
    function allowance(address, address) external pure returns (uint256) { return type(uint256).max; }
    function approve(address, uint256) external pure returns (bool) { return true; }
}

contract RepayNoOpTest is Test {
    Midnight midnight;
    NoOpToken loanToken;

    function testDebtEliminatedWithoutPayment() public {
        midnight = new Midnight(...);
        loanToken = new NoOpToken();

        // 1. Create market with NoOpToken as loanToken
        Market memory market = _buildMarket(address(loanToken));
        bytes32 id = midnight.touchMarket(market);

        // 2. Manufacture debt for borrower (via take or direct state setup in test harness)
        address borrower = address(this);
        uint256 debtUnits = 1000e18;
        // ... set up borrower debt via take() with NoOpToken (also passes safeTransferFrom) ...

        uint256 debtBefore = midnight.debtOf(id, borrower);
        uint256 midnightBalBefore = loanToken.balanceOf(address(midnight)); // 0

        // 3. Call repay — no tokens move
        midnight.repay(market, debtUnits, borrower, address(0), "");

        // Assertions
        assertEq(midnight.debtOf(id, borrower), debtBefore - debtUnits, "debt reduced");
        assertEq(loanToken.balanceOf(address(midnight)), midnightBalBefore, "balance unchanged");
        // withdrawable increased with no backing
        assertEq(midnight.withdrawable(id), debtUnits, "withdrawable inflated");
    }
}
```

Expected: `debtOf` drops by `debtUnits`, `loanToken.balanceOf(address(midnight))` stays at 0, `withdrawable` equals `debtUnits` — proving debt elimination without token receipt.

### Citations

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
