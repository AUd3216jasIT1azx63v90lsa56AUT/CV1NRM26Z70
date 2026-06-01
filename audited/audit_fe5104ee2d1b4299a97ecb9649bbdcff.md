Based on the code at lines 679 and 696–717 of `src/Midnight.sol`, this is a real vulnerability.

### Title
Arbitrary `callback` address used as `payer` without authorization check, enabling third-party fund drain during liquidation - (File: src/Midnight.sol)

### Summary
The `liquidate` function sets `payer = callback` (line 679) when a non-zero `callback` is supplied, then pulls `repaidUnits` of `loanToken` from that address (line 717) after transferring seized collateral to `receiver` (line 696). There is no check that `callback == msg.sender` or that `callback` has authorized `msg.sender` to act on its behalf. Any unprivileged liquidator can therefore name an arbitrary contract as `callback`, causing Midnight to drain that contract's `loanToken` balance to fund the liquidation while the liquidator collects the seized collateral.

### Finding Description
**Code path:** [1](#0-0) 

The `callback` parameter is fully attacker-controlled with no restriction. [2](#0-1) 

`payer` is unconditionally set to `callback` when it is non-zero. No authorization check (e.g., `callback == msg.sender` or `isAuthorized[callback][msg.sender]`) is performed. [3](#0-2) 

Execution order:
1. Seized collateral is transferred to `receiver` (attacker-controlled) at line 696 — **before** any repayment.
2. `onLiquidate` is called on `callback` at lines 698–714. If the victim contract implements `ILiquidateCallback` and returns `CALLBACK_SUCCESS`, the check passes.
3. `safeTransferFrom(loanToken, payer /*= callback*/, address(this), repaidUnits)` at line 717 pulls funds from the victim contract.

**Attacker inputs:**
- `callback` = victim contract address (has `loanToken.approve(midnight, type(uint256).max)` and implements `ILiquidateCallback` returning `CALLBACK_SUCCESS`)
- `receiver` = attacker address
- `borrower` = any liquidatable position

**Why existing checks fail:** [4](#0-3) 

The `liquidatorGate` check (lines 597–600) only gates *who* can call `liquidate`, not *whose funds* are used. It does not prevent an authorized liquidator from naming a victim as `callback`. [5](#0-4) 

The callback return-value check (lines 698–714) only verifies `CALLBACK_SUCCESS` is returned; it does not verify that `callback == msg.sender` or that `callback` consented to being the payer.

### Impact Explanation
A victim contract that (a) holds or has approved `loanToken` to Midnight and (b) implements `ILiquidateCallback` returning `CALLBACK_SUCCESS` loses `repaidUnits` of `loanToken` per liquidation call. The attacker receives `seizedAssets` of collateral at zero personal cost. This is a direct, repeatable theft of the victim contract's tokens.

### Likelihood Explanation
Preconditions are realistic: liquidation-bot contracts and aggregator vaults routinely grant large `loanToken` approvals to Midnight and implement `ILiquidateCallback` to participate in liquidations. A liquidatable borrower must exist (normal market condition). The attack requires no special privilege, no oracle manipulation, and no governance action. It is repeatable as long as the victim contract retains its approval and the callback interface.

### Recommendation
Require that `callback` is either `msg.sender` itself or has explicitly authorized `msg.sender` via the existing `isAuthorized` mapping before accepting it as `payer`:

```solidity
// Before setting payer:
require(
    callback == address(0) || callback == msg.sender || isAuthorized[callback][msg.sender],
    Unauthorized()
);
address payer = callback != address(0) ? callback : msg.sender;
```

Alternatively, always set `payer = msg.sender` and require the liquidator to fund the repayment directly, letting the callback reimburse the liquidator internally — which is the standard flash-liquidation pattern.

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

// Foundry unit test outline
contract VictimCallback is ILiquidateCallback {
    function onLiquidate(...) external returns (bytes32) {
        return CALLBACK_SUCCESS; // unconditionally consents
    }
}

contract LiquidatePayerConfusionTest is Test {
    function test_drainVictimViaCallback() public {
        // Setup: create market, fund borrower, make position liquidatable
        // Give VictimCallback loanToken and approve Midnight max
        uint256 victimBalanceBefore = loanToken.balanceOf(address(victimCallback));
        uint256 attackerCollateralBefore = collateralToken.balanceOf(attacker);

        vm.prank(attacker); // attacker has zero loanToken
        midnight.liquidate(
            market, collateralIndex, seizedAssets, 0,
            borrower, false,
            attacker,           // receiver = attacker
            address(victimCallback), // callback = victim
            ""
        );

        // Assertions:
        assertEq(loanToken.balanceOf(address(victimCallback)), victimBalanceBefore - repaidUnits);
        assertGt(collateralToken.balanceOf(attacker), attackerCollateralBefore);
        assertEq(loanToken.balanceOf(attacker), 0); // attacker spent nothing
    }
}
```

Expected: attacker's collateral balance increases by `seizedAssets`; victim's `loanToken` balance decreases by `repaidUnits`; attacker's `loanToken` balance remains zero.

### Citations

**File:** src/Midnight.sol (L581-591)
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
```

**File:** src/Midnight.sol (L597-600)
```text
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
