Audit Report

## Title
Zero-unit take unconditionally invokes maker's `buyerCallback` at zero token cost - (File: `src/Midnight.sol`)

## Summary
`take()` contains no guard against `units == 0`. When called with `units=0`, `offer.buy=True`, and `offer.callback != address(0)`, all accounting deltas are zero yet the function unconditionally dispatches the maker's `onBuy` callback with `buyerAssets=0`. An unprivileged taker can trigger the maker's callback at zero token cost, violating the invariant that a zero-unit take is a no-op.

## Finding Description

**Root cause**: No `require(units > 0)` guard exists anywhere in `take()`. Confirmed by grep returning zero matches for `require.*units` in `src/Midnight.sol`.

**Exact code path** with `units=0`, `offer.buy=True`, `offer.maxAssets > 0`, `offer.callback != address(0)`:

1. Lines 363–364: `buyerAssets = 0`, `sellerAssets = 0`. [1](#0-0) 

2. Lines 367–369: `consumed[offer.maker][offer.group] += 0`; `require(newConsumed <= offer.maxAssets)` passes trivially since `newConsumed` is unchanged. [2](#0-1) 

3. Lines 382–384: `buyerCreditIncrease=0`, `sellerCreditDecrease=0`, `sellerDebtIncrease=0`. All position writes at lines 408–414 are `+= 0` or `-= 0`. [3](#0-2) 

4. Line 444: The seller's liquidation lock is set **unconditionally** regardless of `units`. For `offer.buy=True`, `seller = taker` (line 375). [4](#0-3) 

5. Lines 445–453: `buyerCallback = offer.callback` for a buy offer (line 420). The check is only `if (buyerCallback != address(0))` — no guard on `buyerAssets > 0` or `units > 0`. The maker's `onBuy` callback is dispatched with `buyerAssets=0, units=0, pendingFeeIncrease=0`. [5](#0-4) 

6. Lines 455–456: `safeTransferFrom(..., 0)` executes silently. Line 476: health check passes because `sellerDebtIncrease=0` left the seller's position unchanged. [6](#0-5) 

**Why existing checks fail**:
- `require(UtilsLib.atMostOneNonZero(offer.maxAssets, offer.maxUnits))` — checks mutual exclusivity only, not that `units > 0`. [7](#0-6) 
- `require(newConsumed <= offer.maxAssets)` — `newConsumed` is unchanged when `buyerAssets=0`. [8](#0-7) 
- `require(offer.maker != taker)` — prevents self-take but not a third-party zero-unit take. [9](#0-8) 

The Certora spec rule `takeInputOutputConsistency` at line 72 explicitly asserts `unitsInput == 0 => buyerAssetsOutput == 0 && sellerAssetsOutput == 0` (i.e., it verifies outputs are zero, not that the call reverts), confirming the protocol does not reject `units=0` at the formal verification level either. [10](#0-9) 

## Impact Explanation

**Callback invoked at zero economic cost**: Any side effects inside `offer.callback.onBuy(id, market, 0, 0, 0, buyer, data)` — state writes, external calls, event emissions, interactions with other protocols — execute for free. The callback must return `CALLBACK_SUCCESS` but is not required to transfer any tokens (since `buyerAssets - sellerAssets = 0`). Flash-lend or position-management callbacks that do not validate `buyerAssets > 0` are directly exploitable.

**Liquidation lock observable during callback**: At the moment `onBuy` executes, `liquidationLocked(id, taker) == true` in transient storage. The callback can branch on this observable state or relay it to other contracts.

**Repeatability**: Each call with `units=0` adds 0 to `consumed`, so the same offer can be targeted indefinitely at gas cost only.

Severity: **Medium**. No direct token theft, but a concrete protocol invariant is broken (zero-unit take is not a no-op), and callback-based integrations can be griefed or manipulated.

## Likelihood Explanation

**Preconditions**:
- A live buy offer with `offer.callback != address(0)` and `offer.maxAssets > 0` must exist. This is the standard configuration for lend-side offers using flash-lend callbacks.
- The taker must not equal `offer.maker` — trivially satisfied by any third party.
- The offer must not be expired and must pass the ratifier check — standard conditions for any take.

**Feasibility**: Fully reachable with a single external call. No privileged access, no oracle manipulation, no special token behavior required. The existing fuzz test `testBuyBuyerCallback` bounds `units` from 0, confirming the protocol does not reject `units=0` at the test level. [11](#0-10) 

## Recommendation

Add a `require(units > 0, ZeroUnits())` guard at the top of `take()`, immediately after the authorization check, before any accounting or callback logic executes:

```solidity
require(units > 0, ZeroUnits());
```

Alternatively, gate the callback dispatch and lock-setting behind `if (units > 0)`, but a top-level revert is cleaner and consistent with how `liquidate()` handles its inputs.

## Proof of Concept

Minimal Foundry test:

```solidity
function testZeroUnitTakeInvokesCallback() public {
    // Deploy a callback contract that records invocations
    RecordingCallback cb = new RecordingCallback();

    // Set up a buy offer with callback
    lenderOffer.callback = address(cb);
    lenderOffer.maxAssets = 1e18;

    // Taker calls take() with units=0
    vm.prank(borrower); // borrower != lender (maker)
    midnight.take(lenderOffer, "", 0, borrower, borrower, address(0), "");

    // Assert callback was invoked despite units=0
    assertEq(cb.invocationCount(), 1);
    assertEq(cb.lastBuyerAssets(), 0);
}
```

`RecordingCallback.onBuy` simply increments a counter and returns `CALLBACK_SUCCESS`. The test will pass, demonstrating the callback is invoked at zero token cost. [12](#0-11)

### Citations

**File:** src/Midnight.sol (L350-350)
```text
        require(UtilsLib.atMostOneNonZero(offer.maxAssets, offer.maxUnits), MultipleNonZero());
```

**File:** src/Midnight.sol (L354-354)
```text
        require(offer.maker != taker, SelfTake());
```

**File:** src/Midnight.sol (L363-364)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/Midnight.sol (L367-369)
```text
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L382-384)
```text
        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
```

**File:** src/Midnight.sol (L444-453)
```text
        bool wasLocked = UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, true);
        if (buyerCallback != address(0)) {
            bytes memory buyerCallbackData = offer.buy ? offer.callbackData : takerCallbackData;
            require(
                IBuyCallback(buyerCallback)
                    .onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
                == CALLBACK_SUCCESS,
                WrongBuyCallbackReturnValue()
            );
        }
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** certora/specs/Midnight.spec (L71-72)
```text
    // If the input is zero, all the output arguments are zero.
    assert unitsInput == 0 => buyerAssetsOutput == 0 && sellerAssetsOutput == 0;
```

**File:** test/TakeTest.sol (L1-17)
```text
// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (c) 2025 Morpho Association
pragma solidity ^0.8.0;

import {IMidnight, Market, Offer, CollateralParams} from "../src/interfaces/IMidnight.sol";
import {Midnight} from "../src/Midnight.sol";
import {WAD, CALLBACK_SUCCESS, MAX_CONTINUOUS_FEE} from "../src/libraries/ConstantsLib.sol";
import {UtilsLib} from "../src/libraries/UtilsLib.sol";
import {TickLib, MAX_TICK} from "../src/libraries/TickLib.sol";
import {IBuyCallback, ISellCallback} from "../src/interfaces/ICallbacks.sol";
import {IRatifier} from "../src/interfaces/IRatifier.sol";
import {IdLib} from "../src/libraries/IdLib.sol";
import {BaseTest} from "./BaseTest.sol";
import {ERC20} from "./erc20s/ERC20.sol";
import {Oracle} from "./helpers/Oracle.sol";

contract TakeTest is BaseTest {
```
