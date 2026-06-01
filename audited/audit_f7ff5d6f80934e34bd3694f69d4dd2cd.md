### Title
Fee-on-Transfer Loan Token Causes `withdrawable` Over-Credit in `liquidate`, Breaking Solvency Invariant - (File: src/Midnight.sol)

### Summary
The `liquidate` function increments `marketState[id].withdrawable` and decrements `position[id][borrower].debt` by the full `repaidUnits` before calling `SafeTransferLib.safeTransferFrom`, which does not verify the actual balance delta received. When the market's `loanToken` is a fee-on-transfer token, the protocol receives only `repaidUnits - fee` but credits lenders with `repaidUnits`, permanently breaking the invariant that `IERC20(loanToken).balanceOf(address(this)) >= sum(withdrawable[id])`. Because market creation is permissionless and accepts an arbitrary `loanToken` with no on-chain validation, any unprivileged market creator can deploy this configuration.

### Finding Description

**Permissionless market creation with arbitrary loan token:**

`touchMarket` (lines 754–791) is callable by anyone and performs no validation on `market.loanToken` — no code check, no interface check, no fee-on-transfer guard. [1](#0-0) [2](#0-1) 

**State mutation before transfer in `liquidate`:**

Lines 675–676 update `withdrawable` and `debt` unconditionally before any token transfer occurs: [3](#0-2) 

The actual transfer happens at line 717, after the collateral transfer and optional callback: [4](#0-3) 

**`SafeTransferLib.safeTransferFrom` does not check balance delta:**

It only verifies the call did not revert and the return value is truthy. A fee-on-transfer token satisfies both conditions while delivering `repaidUnits - fee` to the contract. [5](#0-4) 

**Exploit flow:**

1. Attacker (market creator) calls `touchMarket` with a fee-on-transfer ERC20 as `loanToken`.
2. Lenders supply via `take`; borrowers borrow via `take`. (The same fee-on-transfer issue also affects `take`'s inbound transfer, but the discrepancy compounds here.)
3. Borrower's position becomes unhealthy (oracle price drop) or maturity passes.
4. Liquidator (may be the same attacker or any third party) calls `liquidate(..., repaidUnits, ...)`.
5. Line 675: `withdrawable += repaidUnits` — full amount credited.
6. Line 676: `debt -= repaidUnits` — full debt erased.
7. Line 717: contract receives only `repaidUnits - fee`.
8. `withdrawable` now exceeds the contract's actual token balance for this market.
9. The last lenders to call `withdraw` find the contract insolvent; `safeTransfer` reverts or drains tokens belonging to other markets sharing the same token.

**No existing check stops this:** `UtilsLib.atMostOneNonZero`, the `NotLiquidatable` guard, the RCF check, and the `liquidatorGate` all operate on units/collateral logic and are orthogonal to token receipt accounting. [6](#0-5) 

### Impact Explanation

After one or more liquidations with a fee-on-transfer loan token, `marketState[id].withdrawable` exceeds the contract's actual `loanToken` balance attributable to that market. Lenders calling `withdraw` will receive tokens that belong to other depositors or other markets, or the call will revert entirely. This is direct, concrete lender fund loss and protocol insolvency, exactly matching the scoped impact. [7](#0-6) 

### Likelihood Explanation

**Preconditions:**
- A market must be created with a fee-on-transfer loan token (permissionless, zero cost).
- At least one liquidation with `repaidUnits > 0` must occur (normal protocol operation).

**Feasibility:** High. Market creation is fully permissionless with no token validation. Fee-on-transfer tokens are a well-known, deployed token class (e.g., PAXG, STA, tokens with configurable transfer fees). The liquidator need not be the attacker; any liquidation of any unhealthy position in such a market triggers the discrepancy. The attack is repeatable on every liquidation and every `repay` call in the same market. [8](#0-7) 

### Recommendation

Adopt a balance-before/after pattern for all inbound loan token transfers. Replace the direct `safeTransferFrom` call with a check that computes the actual received amount:

```solidity
uint256 balanceBefore = IERC20(market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
uint256 received = IERC20(market.loanToken).balanceOf(address(this)) - balanceBefore;
require(received == repaidUnits, InsufficientTransfer());
```

Apply the same fix to `repay` (line 520) and to the buyer-side transfer in `take`. Alternatively, add an on-chain check in `touchMarket` that performs a zero-value self-transfer probe and reverts if the token takes a fee, or document and enforce via a token allowlist. [9](#0-8) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight, Market, CollateralParams} from "src/Midnight.sol";
import {IERC20} from "src/interfaces/IERC20.sol";

/// @dev Minimal fee-on-transfer ERC20: charges 10% on every transferFrom.
contract FeeToken {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    uint256 constant FEE_BPS = 1000; // 10%

    function mint(address to, uint256 amt) external { balanceOf[to] += amt; }
    function approve(address sp, uint256 amt) external returns (bool) {
        allowance[msg.sender][sp] = amt; return true;
    }
    function transfer(address to, uint256 amt) external returns (bool) {
        balanceOf[msg.sender] -= amt; balanceOf[to] += amt; return true;
    }
    function transferFrom(address from, address to, uint256 amt) external returns (bool) {
        allowance[from][msg.sender] -= amt;
        uint256 fee = amt * FEE_BPS / 10000;
        balanceOf[from] -= amt;
        balanceOf[to] += amt - fee; // fee burned
        return true;
    }
}

contract FeeOnTransferLiquidateTest is Test {
    Midnight midnight;
    FeeToken loanToken;
    // ... standard test setup (oracle, collateral token, etc.)

    function testFeeOnTransferLiquidateInsolvency() public {
        // 1. Deploy fee-on-transfer loan token
        loanToken = new FeeToken();

        // 2. Create market permissionlessly with fee token as loanToken
        Market memory market = _buildMarket(address(loanToken));
        bytes32 id = midnight.touchMarket(market);

        // 3. Lender supplies N units via take (lender gets credit = N)
        uint256 N = 1000e18;
        _lenderSupply(market, id, N);

        // 4. Borrower borrows N units (borrower gets debt = N)
        _borrowerBorrow(market, id, N);

        // 5. Make position unhealthy (drop oracle price)
        _makeUnhealthy();

        // 6. Liquidator liquidates with repaidUnits = R
        uint256 R = 500e18;
        uint256 balBefore = loanToken.balanceOf(address(midnight));
        loanToken.mint(address(this), R);
        loanToken.approve(address(midnight), R);
        midnight.liquidate(market, 0, 0, R, borrower, false, address(this), address(0), "");

        uint256 balAfter = loanToken.balanceOf(address(midnight));
        uint256 actualReceived = balAfter - balBefore; // = R * 0.9 = 450e18
        uint128 withdrawableAfter = midnight.withdrawable(id);

        // KEY ASSERTION: withdrawable was incremented by R but only R*0.9 received
        // withdrawableAfter == withdrawableBefore + R  (over-credited by fee = 50e18)
        assertGt(withdrawableAfter, balAfter, "withdrawable exceeds balance: INSOLVENCY");
        assertEq(actualReceived, R * 9 / 10, "only 90% received due to fee");
        // Lenders cannot all withdraw: last lender loses `fee` tokens
    }
}
```

**Expected assertions:**
- `withdrawable(id) > IERC20(loanToken).balanceOf(address(midnight))` — solvency invariant violated.
- `actualReceived == repaidUnits * 0.9` — confirms fee-on-transfer behavior.
- A subsequent `withdraw` by the last lender reverts or steals from another depositor.

### Citations

**File:** src/Midnight.sol (L494-499)
```text
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

**File:** src/Midnight.sol (L595-600)
```text
        require(UtilsLib.atMostOneNonZero(repaidUnits, seizedAssets), InconsistentInput());
        require(_position.debt > 0, NotBorrower()); // to avoid no-op liquidations of non borrower positions.
        require(
            market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
            LiquidatorGatedFromLiquidating()
        );
```

**File:** src/Midnight.sol (L675-676)
```text
            _marketState.withdrawable += UtilsLib.toUint128(repaidUnits);
            _position.debt -= UtilsLib.toUint128(repaidUnits);
```

**File:** src/Midnight.sol (L717-717)
```text
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
```

**File:** src/Midnight.sol (L754-791)
```text
    /// @dev Returns the market id and creates the market if it doesn't exist yet.
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

**File:** live_context.json (L15-15)
```json
      "permissionless market creation",
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
