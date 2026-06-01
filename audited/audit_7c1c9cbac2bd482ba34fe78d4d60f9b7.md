### Title
Zero-`buyerAssets` rounding silently skips `consumed` increment on buy offers with `maxAssets > 0`, allowing unlimited free fills - (File: src/Midnight.sol)

### Summary
When a buy offer sets `maxAssets > 0`, the `consumed` mapping is incremented by `buyerAssets`, which is computed as `units.mulDivDown(buyerPrice, WAD)`. At very low tick prices (minimum `PRICE_ROUNDING_STEP = 1e12`), any `units` value below `WAD / price = 1e6` produces `buyerAssets = 0` via integer truncation. Because `consumed` is incremented by zero, the `maxAssets` cap is never approached, and the offer can be filled an unlimited number of times with no token cost to the maker while the maker's credit position grows with each call.

### Finding Description

**Exact code path** — `src/Midnight.sol` lines 363–369:

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
// ...
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
``` [1](#0-0) 

For a buy offer, `buyerPrice = offerPrice` (the settlement fee cancels out: `sellerPrice = offerPrice - fee`, `buyerPrice = sellerPrice + fee`). `mulDivDown` is plain integer division:

```solidity
function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y) / d;
}
``` [2](#0-1) 

`tickToPrice` rounds prices to multiples of `PRICE_ROUNDING_STEP = 1e12`, so the minimum reachable price is `1e12` (at tick = 0): [3](#0-2) [4](#0-3) 

With `offerPrice = 1e12` and `units = 999_999`:
- `buyerAssets = 999_999 * 1e12 / 1e18 = 999_999e12 / 1e18 = 0` (truncated)
- `sellerAssets = 0` (same price, same truncation)
- `consumed[maker][group] += 0` → **consumed unchanged**
- `require(0 <= maxAssets)` → **passes**

Position mutations still execute unconditionally after the consumed block:

```solidity
buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);   // maker gains units credit
sellerPos.debt   += UtilsLib.toUint128(sellerDebtIncrease);   // taker gains units debt
``` [5](#0-4) 

Token transfers are also zero:

```solidity
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets); // 0
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);                    // 0
``` [6](#0-5) 

Because `consumed` never increments, the same offer can be taken again immediately with another `units = 999_999` call, and again, indefinitely.

**Attacker-controlled inputs:**
- `offer.buy = true`, `offer.maxAssets > 0`, `offer.tick = 0` (or any tick where `price < WAD`)
- `units` in `[1, WAD/price − 1]` (e.g., `[1, 999_999]` at minimum price)
- Taker is a second address controlled by the maker (bypasses `SelfTake` check)

**Why existing checks fail:**
- `require(newConsumed <= offer.maxAssets)`: `newConsumed = 0` always passes
- `require(offer.maker != taker, SelfTake())`: only blocks identical addresses; a second maker-controlled address is not blocked
- No guard requiring `buyerAssets > 0` when `units > 0`
- Certora spec `takeConsumedDelta` only covers the `maxAssets == 0` branch (line 68: `require offer.maxAssets == 0`) [7](#0-6) 

### Impact Explanation

The maker's buy offer with `maxAssets > 0` is filled an unlimited number of times at zero asset cost. Each fill mutates `buyerPos.credit` (maker gains credit) and `sellerPos.debt` (taker gains debt) without any token transfer and without advancing `consumed`. The `maxAssets` cap — the sole replay-prevention mechanism for asset-denominated offers — is permanently bypassed. This violates the core invariant: "offers cannot be replayed, overfilled, reused, or filled after cancel/deadline."

### Likelihood Explanation

**Preconditions:**
1. A buy offer exists with `maxAssets > 0` and a tick whose price satisfies `price < WAD` (any tick below `MAX_TICK = 5820` qualifies; the minimum price `1e12` is reachable at tick 0).
2. The maker controls a second address to act as taker (or has an accomplice).
3. No minimum-units check exists, so `units = 1` already triggers the bug at sufficiently low prices.

The condition is reachable on any market with low-tick offers and is repeatable without limit in a single block. The maker bears no token cost per fill.

### Recommendation

Add a guard in the `maxAssets` branch that rejects a non-zero `units` fill that produces zero consumed increment:

```solidity
if (offer.maxAssets > 0) {
    uint256 consumedDelta = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || consumedDelta > 0, ZeroConsumedIncrement());
    newConsumed = consumed[offer.maker][offer.group] += consumedDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, enforce a minimum `units` value such that `buyerAssets >= 1` is guaranteed, or track consumed in `units` (not assets) even when `maxAssets > 0`, converting the cap at check time.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight, Offer} from "src/Midnight.sol";
import {TickLib} from "src/libraries/TickLib.sol";

contract ZeroConsumedFuzz is Test {
    // Fuzz: price in [PRICE_ROUNDING_STEP, WAD-1], units in [1, WAD/price - 1]
    function testFuzz_zeroConsumedBuyOffer(uint256 tick, uint256 units) public {
        // Setup: deploy Midnight, create market, configure ratifier
        // ...

        uint256 price = TickLib.tickToPrice(tick % 5820); // any valid tick
        vm.assume(price > 0 && price < 1e18);
        uint256 maxUnitsForZero = 1e18 / price; // units below this → buyerAssets = 0
        vm.assume(maxUnitsForZero > 1);
        units = bound(units, 1, maxUnitsForZero - 1);

        // Maker creates buy offer with maxAssets > 0
        Offer memory offer;
        offer.buy = true;
        offer.maxAssets = 1e30; // large cap
        offer.tick = tick % 5820;
        // ... set market, ratifier, expiry, group

        uint256 consumedBefore = midnight.consumed(offer.maker, offer.group);

        // Taker (maker's second address) calls take
        vm.prank(takerAddress);
        (uint256 buyerAssets,) = midnight.take(offer, hex"", units, takerAddress, takerAddress, address(0), hex"");

        uint256 consumedAfter = midnight.consumed(offer.maker, offer.group);

        // Core assertion: if buyerAssets == 0 and units > 0, consumed must still have incremented
        if (buyerAssets == 0 && units > 0) {
            // BUG: this assertion FAILS on current code
            assertGt(consumedAfter, consumedBefore, "consumed must increment on any non-zero fill");
        }

        // Invariant: credit increase implies consumed increase
        uint256 makerCredit = midnight.creditOf(id, offer.maker);
        if (makerCredit > 0) {
            assertGt(consumedAfter, consumedBefore, "credit increase without consumed increment");
        }
    }
}
```

**Expected result on current code:** `assertGt` fails — `consumedAfter == consumedBefore` while `buyerAssets == 0` and `makerCredit > 0`, confirming the invariant violation.

### Citations

**File:** src/Midnight.sol (L363-369)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L408-414)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
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

**File:** src/libraries/ConstantsLib.sol (L8-8)
```text
uint256 constant WAD = 1e18;
```

**File:** certora/specs/Consume.spec (L67-75)
```text
rule takeConsumedDelta(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxAssets == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert consumed(offer.maker, offer.group) == consumedBefore + units;
}
```
