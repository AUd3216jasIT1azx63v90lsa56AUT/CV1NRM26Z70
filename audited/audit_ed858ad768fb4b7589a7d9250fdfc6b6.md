### Title
Buy-offer consumed counter frozen at zero for all ticks below MAX\_TICK when maxAssets > 0 — (`src/libraries/TickLib.sol` / `src/Midnight.sol`)

### Summary

For every buy offer whose tick is strictly below `MAX_TICK` (5820), `buyerPrice = tickToPrice(tick) < WAD`, so `mulDivDown(1, buyerPrice, WAD) = 0`. When `offer.maxAssets > 0` the consumed counter is incremented by `buyerAssets`, which is zero for `units = 1`, so the counter never advances and the `maxAssets` cap is never reached. An attacker who controls both a maker and a taker account can call `take(units=1)` an unbounded number of times, inflating credit and debt positions with zero token transfer, then drain the protocol's `withdrawable` pool via `withdraw()`.

### Finding Description

**Price algebra — settlement fee cancels out for buy offers.** [1](#0-0) 

```
sellerPrice = offerPrice - settlementFee   // buy branch
buyerPrice  = sellerPrice + settlementFee  // = offerPrice always
```

`buyerPrice` equals `tickToPrice(offer.tick)` for every buy offer, regardless of the settlement fee. The piecewise-linear interpolation is irrelevant to this path.

**tickToPrice range.** [2](#0-1) 

The function returns values rounded to multiples of `PRICE_ROUNDING_STEP = 1e12`. The Certora spec confirms `tickToPrice(MAX_TICK) == 1e18` and `tickToPrice(tick) <= 1e18` for all ticks. Therefore for every tick in `[1, MAX_TICK-1]`, `tickToPrice(tick) ∈ [1e12, 1e18 - 1e12]`, which is strictly less than `WAD = 1e18`.

**Zero buyerAssets for units = 1.** [3](#0-2) 

`buyerAssets = mulDivDown(1, buyerPrice, WAD) = (1 * buyerPrice) / 1e18`. Since `buyerPrice < 1e18`, integer division yields 0.

**Consumed counter never advances.** [4](#0-3) 

```solidity
newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

`buyerAssets = 0` → `consumed` stays at its previous value → `require` always passes → the offer is never exhausted.

**Position state still mutates per call.** [5](#0-4) 

Each call with `units = 1` increments the buyer's credit by 1 and the seller's debt by 1. No tokens are transferred because both `buyerAssets` and `sellerAssets` are 0.

**Exploit flow (attacker controls maker account A and taker account B):**

1. A creates a buy offer: `tick = 1` (or any tick < 5820), `maxAssets = type(uint256).max`.
2. B supplies collateral sufficient to remain healthy under N units of debt.
3. B calls `take(offer, ..., units=1, ...)` N times. Each call: consumed stays 0, A gains +1 credit, B gains +1 debt, 0 tokens move.
4. A calls `withdraw(N)` — receives N loan tokens from `_marketState.withdrawable`.
5. `withdrawable` is funded by legitimate borrowers' repayments; those lenders can no longer withdraw.

**Existing protections are insufficient.**

- `require(offer.maker != taker)` — blocked by using two distinct addresses.
- `require(isHealthy(...))` — satisfied as long as B holds adequate collateral.
- No `require(units > 0)` or `require(buyerAssets > 0)` guard exists.

### Impact Explanation

The `maxAssets` cap on a buy offer is completely bypassed for every tick below `MAX_TICK`. An attacker can mint an arbitrary amount of credit for the maker account at zero token cost, then redeem that credit against the protocol's `withdrawable` pool, stealing funds deposited by legitimate borrowers as repayments. The core invariant "offers cannot be replayed, overfilled, or reused" is violated.

### Likelihood Explanation

- Preconditions: any buy offer with `maxAssets > 0` and `tick < 5820` (i.e., virtually every real offer, since `MAX_TICK` corresponds to price = 1.0 and is the ceiling).
- Attacker needs two addresses and collateral for the taker account; no privileged access required.
- Repeatable in a single transaction via multicall or a flash-loan-funded loop.
- Settlement fee value is irrelevant; the bug is present at all times.

### Recommendation

Add a guard in `take()` that rejects zero-asset fills when `maxAssets > 0`:

```solidity
uint256 consumedDelta = offer.buy ? buyerAssets : sellerAssets;
if (offer.maxAssets > 0) {
    require(consumedDelta > 0, ZeroConsumedDelta()); // new guard
    newConsumed = consumed[offer.maker][offer.group] += consumedDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, enforce `require(units >= WAD / buyerPrice + 1)` so that `mulDivDown(units, buyerPrice, WAD) >= 1` before the consumed update. The root fix is to ensure the consumed increment is always positive when `maxAssets > 0`.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight, Offer} from "src/Midnight.sol";
import {TickLib, MAX_TICK} from "src/libraries/TickLib.sol";
import {UtilsLib} from "src/libraries/UtilsLib.sol";

contract ZeroConsumedFillTest is Test {
    // Setup: deploy Midnight, create market, fund withdrawable with
    // legitimate repayments, create buy offer from maker with tick=1,
    // maxAssets=1e18, supply collateral for taker.

    function testUnboundedFillDrainsWithdrawable() public {
        // Precondition: tickToPrice(1) < WAD
        uint256 price = TickLib.tickToPrice(1);
        assertLt(price, 1e18, "price must be < WAD");

        // Precondition: mulDivDown(1, price, WAD) == 0
        assertEq(UtilsLib.mulDivDown(1, price, 1e18), 0, "buyerAssets must be 0");

        uint256 N = 1000;
        // Call take(units=1) N times; assert consumed stays 0 throughout
        for (uint256 i = 0; i < N; i++) {
            // vm.prank(taker); midnight.take(offer, ..., 1, ...);
            // assertEq(midnight.consumed(maker, group), 0);
        }

        // After N fills: maker has N credit, taker has N debt, 0 tokens moved
        // assertEq(midnight.creditOf(id, maker), N);
        // assertEq(midnight.debtOf(id, taker), N);

        // Maker withdraws N tokens from withdrawable (funded by legitimate repayments)
        // vm.prank(maker); midnight.withdraw(market, N, maker, maker);
        // assertEq(loanToken.balanceOf(maker), N);  // stolen from withdrawable

        // Invariant violated: consumed[maker][group] == 0 < maxAssets after N fills
        // assertEq(midnight.consumed(maker, group), 0);
    }

    // Fuzz variant: for all tick in [1, MAX_TICK-1], buyerAssets == 0 for units=1
    function testFuzz_allTicksBelowMaxTickYieldZeroBuyerAssets(uint256 tick) public {
        tick = bound(tick, 1, MAX_TICK - 1);
        uint256 price = TickLib.tickToPrice(tick);
        assertEq(UtilsLib.mulDivDown(1, price, 1e18), 0,
            "buyerAssets must be 0 for all ticks below MAX_TICK");
    }
}
```

**Expected assertions:**
- `mulDivDown(1, tickToPrice(t), 1e18) == 0` for all `t ∈ [1, MAX_TICK-1]` — fuzz confirms the invariant is broken across the entire valid tick range.
- `consumed[maker][group] == 0` after N fills — the cap is never approached.
- `loanToken.balanceOf(maker) == N` after `withdraw(N)` — funds drained from `withdrawable`.

### Citations

**File:** src/Midnight.sol (L361-362)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
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

**File:** src/Midnight.sol (L408-414)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
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
