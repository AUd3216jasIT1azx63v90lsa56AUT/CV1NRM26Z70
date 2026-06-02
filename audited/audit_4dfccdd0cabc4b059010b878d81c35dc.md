Audit Report

## Title
Fully-consumed assets-mode buy offer can be re-taken with `units > 0` when `buyerPrice < WAD`, minting unbacked credit and debt - (`File: src/Midnight.sol`, `src/libraries/TickLib.sol`)

## Summary
In `Midnight.take`, when a buy offer uses `maxAssets` mode, `buyerAssets` is computed as `mulDivDown(units, buyerPrice, WAD)`. Because `tickToPrice` always returns a value strictly less than `WAD` for every valid tick, this expression evaluates to `0` for `units = 1`. A fully-consumed offer therefore passes the `require(newConsumed <= offer.maxAssets)` guard indefinitely while still executing the full position-accounting path, minting one unit of unbacked credit to the maker and one unit of debt to the taker per call with zero token transfers.

## Finding Description

**Root cause — `src/Midnight.sol` lines 363–369:**

`buyerAssets` for a buy offer is computed as `units.mulDivDown(buyerPrice, WAD)`. [1](#0-0) 

When `units = 1` and `buyerPrice < WAD`, `mulDivDown(1, buyerPrice, WAD) = floor(buyerPrice / WAD) = 0`. The consumed guard then computes `newConsumed = M + 0 = M` and `require(M <= M)` passes unconditionally.

**Why `buyerPrice < WAD` always holds — `src/libraries/TickLib.sol` lines 44–51:**

`tickToPrice` divides `1e36` by `(1e18 + wExp(...))`. Since `wExp(...)` is always strictly positive, the denominator is always strictly greater than `1e18`, making the result always strictly less than `1e18 = WAD` for all 5820 valid ticks. [2](#0-1) 

**Position accounting still executes with zero token transfers:**

Lines 382–414 run unconditionally after the consumed guard. With `units = 1`, `buyerCreditIncrease = zeroFloorSub(1, buyerPos.debt) = 1` (if maker has no debt), `sellerDebtIncrease = 1`. `buyerPos.credit += 1` and `sellerPos.debt += 1` are written. [3](#0-2) 

At lines 455–456, `safeTransferFrom(..., buyerAssets - sellerAssets)` and `safeTransferFrom(..., sellerAssets)` both transfer `0` tokens since `buyerAssets = sellerAssets = 0`. [4](#0-3) 

**Why existing checks fail:**

The `takeConsumedAtMaxUnchangedAssets` Certora rule (lines 88–97) only asserts that `consumed` is unchanged when already at max — trivially satisfied since `buyerAssets = 0` — but does not assert `units == 0`. [5](#0-4) 

The `fullyConsumedOfferRevertsOnNonTrivialTake` rule (lines 99–111) only covers `maxAssets == 0` (units mode); no equivalent rule exists for assets mode. [6](#0-5) 

The protocol's own NatDoc at `src/Midnight.sol` line 94 explicitly acknowledges: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* The test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` confirms this is a reachable, known state named as a bug. [7](#0-6) 

## Impact Explanation

Each successful re-take with `units = 1` on a fully-consumed buy offer mints 1 unit of credit to the maker without any corresponding loan-token deposit, and 1 unit of debt to the taker without any loan-token transfer. `totalUnits` is also incremented at line 417, inflating the continuous-fee accrual base for all lenders. [8](#0-7) 

The contract's loan-token balance no longer covers all outstanding credit, violating the core protocol invariant that contract balances cover credit redemption and withdrawable assets. The `maxAssets` cap is rendered permanently ineffective. This constitutes direct protocol insolvency through unbounded unbacked credit/debt minting, triggerable by any unprivileged user.

## Likelihood Explanation

**Preconditions:**
- A buy offer with `maxAssets > 0` at any valid tick (structural: holds for all 5820 ticks).
- `consumed[maker][group] == maxAssets` (normal end state after full consumption).
- Attacker is any unprivileged address that is not the maker (only the `SelfTake` guard applies).

**Feasibility:** High. The `buyerPrice < WAD` condition is structural and permanent — no oracle manipulation, no admin action, no special token behavior required. The attacker only needs to call `take` with `units = 1` after any buy offer is exhausted.

**Repeatability:** Unlimited. Each call succeeds and adds 1 unit of unbacked credit/debt. The consumed counter stays pinned at `maxAssets` indefinitely.

## Recommendation

Add a guard immediately after computing `buyerAssets` (or `sellerAssets`) to require that a non-zero `units` input produces a non-zero asset delta:

```solidity
require(units == 0 || (offer.buy ? buyerAssets : sellerAssets) > 0, ZeroAssetTake());
```

Alternatively, enforce that `units` must be zero when the offer is fully consumed in assets mode, mirroring the behavior of `fullyConsumedOfferRevertsOnNonTrivialTake` for units mode. The Certora rule `takeConsumedAtMaxUnchangedAssets` should also be strengthened to assert `units == 0` rather than only asserting `consumed` is unchanged.

## Proof of Concept

1. Deploy with any valid market and create a buy offer with `maxAssets = M > 0` at any tick (e.g., tick 0).
2. Fill the offer normally until `consumed[maker][group] == M`.
3. Call `take(units=1, ...)` as any unprivileged address (not the maker).
4. Observe: `buyerAssets = 0`, `sellerAssets = 0`, no token transfer occurs, but `buyerPos.credit` increases by 1 and `sellerPos.debt` increases by 1.
5. Repeat step 3 unboundedly; `consumed` stays pinned at `M` and credit/debt grow without bound.

The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` provides a direct reproduction path. [9](#0-8)

### Citations

**File:** src/Midnight.sol (L93-94)
```text
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L363-369)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L382-414)
```text
        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
        uint128 buyerPendingFeeIncrease =
            UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
        uint128 sellerPendingFeeDecrease = sellerPos.credit > 0
            ? UtilsLib.toUint128(sellerPos.pendingFee.mulDivUp(sellerCreditDecrease, sellerPos.credit))
            : 0;

        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
        require(
            !offer.reduceOnly || (offer.buy ? buyerCreditIncrease == 0 : sellerDebtIncrease == 0),
            MakerCreditOrDebtIncreased()
        );

        require(
            offer.market.enterGate == address(0) || buyerCreditIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseCredit(buyer),
            BuyerGatedFromIncreasingCredit()
        );
        require(
            offer.market.enterGate == address(0) || sellerDebtIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseDebt(seller),
            SellerGatedFromIncreasingDebt()
        );

        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/Midnight.sol (L416-417)
```text
        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/libraries/TickLib.sol (L44-51)
```text
    function tickToPrice(uint256 tick) internal pure returns (uint256) {
        require(tick <= MAX_TICK, TickOutOfRange());
        unchecked {
            // forge-lint: disable-next-item(unsafe-typecast)
            return uint256(1e36)
                    .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
                    .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
        }
```

**File:** certora/specs/Consume.spec (L88-97)
```text
/// If consumed is already at or above maxAssets before a take in assets mode, it remains unchanged.
rule takeConsumedAtMaxUnchangedAssets(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxUnits == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert consumedBefore >= offer.maxAssets => consumed(offer.maker, offer.group) == consumedBefore;
}
```

**File:** certora/specs/Consume.spec (L99-111)
```text
/// A fully-consumed offer in units mode only allows no-op takes.
rule fullyConsumedOfferRevertsOnNonTrivialTake(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxAssets == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    require offer.maxUnits > 0 && consumedBefore >= offer.maxUnits, "assume the offer is fully consumed";

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    // If take does not revert, its input has to be zero.
    assert units == 0;
}
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
