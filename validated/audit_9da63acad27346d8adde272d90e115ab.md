Audit Report

## Title
Fully-consumed assets-mode buy offer can be re-taken with `units > 0` when `buyerPrice < WAD`, minting unbacked credit and debt - (`File: src/Midnight.sol`, `src/libraries/TickLib.sol`)

## Summary
In `Midnight.take`, when a buy offer uses `maxAssets` mode, `buyerAssets` is computed as `mulDivDown(units, buyerPrice, WAD)`. Since `tickToPrice` always returns a value strictly less than `WAD` for all 5820 valid ticks, this evaluates to `0` for `units = 1`. A fully-consumed offer therefore passes the `require(newConsumed <= offer.maxAssets)` guard indefinitely while the full position-accounting path still executes, minting one unit of unbacked credit to the maker and one unit of debt to the taker per call with zero token transfers.

## Finding Description

**Root cause — `src/Midnight.sol` lines 363–373:**

The consumed-assets delta for a buy offer is:

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // rounds DOWN → 0 when units=1
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
``` [1](#0-0) 

When `units = 1` and `buyerPrice < WAD`, `mulDivDown(1, buyerPrice, WAD) = floor(buyerPrice / WAD) = 0`. The consumed counter therefore never advances past `maxAssets`, making the cap permanently bypassable.

**Why `buyerPrice < WAD` always holds — `src/libraries/TickLib.sol` lines 44–52:**

```solidity
return uint256(1e36)
    .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
    .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
``` [2](#0-1) 

The denominator `1e18 + wExp(...)` is always strictly greater than `1e18`, so the result is always strictly less than `1e18 = WAD` for all valid ticks. This is a structural, permanent property.

**Position accounting still executes with `units = 1` — `src/Midnight.sol` lines 382–417:**

```solidity
uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
uint256 sellerDebtIncrease = units - sellerCreditDecrease;
...
buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);   // +1, no tokens deposited
sellerPos.debt   += UtilsLib.toUint128(sellerDebtIncrease);   // +1, no tokens received
_marketState.totalUnits += buyerCreditIncrease - sellerCreditDecrease; // +1
``` [3](#0-2) 

**Token transfers are zero — `src/Midnight.sol` lines 455–456:**

```solidity
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets); // 0
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);                    // 0
``` [4](#0-3) 

**Why existing checks fail:**

The Certora rule `takeConsumedAtMaxUnchangedAssets` (`certora/specs/Consume.spec` lines 88–97) only asserts that `consumed` is unchanged when already at max — trivially satisfied since `buyerAssets = 0` — but does not assert `units == 0`. [5](#0-4) 

The rule `fullyConsumedOfferRevertsOnNonTrivialTake` (lines 99–111) only covers `maxAssets == 0` (units mode); no equivalent rule exists for assets mode. [6](#0-5) 

The protocol's own NatDoc at `src/Midnight.sol` line 94 explicitly acknowledges: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* — confirming the team is aware of the behavior but has not guarded against it. [7](#0-6) 

**Exploit flow:**
1. A buy offer exists with `offer.buy = true`, `offer.maxAssets = M > 0`, any valid tick.
2. `consumed[maker][group]` reaches `M` through normal fills.
3. Attacker calls `take(units=1, ...)`:
   - `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`
   - `newConsumed = M + 0 = M`; `require(M <= M)` passes
   - `buyerPos.credit += 1` — maker receives 1 unit of credit, no loan tokens deposited
   - `sellerPos.debt += 1` — taker receives 1 unit of debt, no loan tokens received
   - `totalUnits += 1`
   - No token transfers occur
4. Step 3 repeats indefinitely; `consumed` stays pinned at `M` forever.

## Impact Explanation

Each successful re-take mints 1 unit of credit to the maker without any corresponding loan-token deposit, and 1 unit of debt to the taker without any loan-token transfer. It also increments `totalUnits`, inflating the continuous-fee accrual base for all lenders. The contract's loan-token balance no longer covers all outstanding credit, violating the core protocol invariant that contract balances cover credit redemption and withdrawable assets. The `maxAssets` cap — intended to bound the maker's total exposure — is rendered permanently ineffective. This constitutes direct protocol insolvency through unbounded unbacked credit/debt minting, triggerable by any unprivileged user.

## Likelihood Explanation

**Preconditions:**
- A buy offer with `maxAssets > 0` at any valid tick (structural: holds for all 5820 ticks).
- `consumed[maker][group] == maxAssets` (normal end state after full consumption).
- Attacker is any unprivileged address that is not the maker (only the `SelfTake` guard applies).

**Feasibility:** High. The `buyerPrice < WAD` condition is structural and permanent — no oracle manipulation, no admin action, no special token behavior required. The attacker only needs to call `take` with `units = 1` after any offer is exhausted.

**Repeatability:** Unlimited. Each call succeeds and adds 1 unit of unbacked credit/debt. The consumed counter stays pinned at `maxAssets` indefinitely.

## Recommendation

Add a guard in the `maxAssets` branch that rejects non-zero `units` when the computed asset delta is zero:

```solidity
if (offer.maxAssets > 0) {
    uint256 assetsDelta = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || assetsDelta > 0, ZeroAssetDelta());
    newConsumed = consumed[offer.maker][offer.group] += assetsDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, enforce a minimum tick price such that `mulDivDown(1, tickToPrice(tick), WAD) > 0`, or enforce `units >= WAD / tickToPrice(tick)` when `maxAssets > 0`. The Certora rule `fullyConsumedOfferRevertsOnNonTrivialTake` should be extended to cover the `maxAssets > 0` case.

## Proof of Concept

The test `testBugBuyMaxAssetsBypass` already exists in `test/TakeTest.sol` and confirms this is a reachable state. A minimal reproduction:

1. Create a buy offer with `maxAssets = M`, any valid tick (e.g., tick 0, where `tickToPrice(0)` is the minimum price, well below WAD).
2. Fill the offer normally until `consumed[maker][group] == M`.
3. Call `take(units=1, ...)` as any unprivileged address.
4. Assert: call succeeds, `consumed` is still `M`, `buyerPos.credit` increased by 1, `sellerPos.debt` increased by 1, no tokens transferred.
5. Repeat step 3 N times; assert `buyerPos.credit` increased by N with zero token inflow.

### Citations

**File:** src/Midnight.sol (L93-94)
```text
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L363-373)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
```

**File:** src/Midnight.sol (L382-417)
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

        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/libraries/TickLib.sol (L44-52)
```text
    function tickToPrice(uint256 tick) internal pure returns (uint256) {
        require(tick <= MAX_TICK, TickOutOfRange());
        unchecked {
            // forge-lint: disable-next-item(unsafe-typecast)
            return uint256(1e36)
                    .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
                    .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
        }
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
