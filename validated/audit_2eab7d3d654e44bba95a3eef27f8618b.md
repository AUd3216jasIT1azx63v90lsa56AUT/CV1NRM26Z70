Audit Report

## Title
Buy-offer `consumed` accounting truncates to zero when `units * buyerPrice < WAD`, rendering `maxAssets` cap unenforceable - (File: src/Midnight.sol)

## Summary
When a buy offer has `maxAssets > 0` and a taker fills `units = 1` at any tick below `MAX_TICK`, `buyerAssets = mulDivDown(1, buyerPrice, WAD)` evaluates to `0` because `buyerPrice < WAD` for all ticks except `MAX_TICK`. The `consumed` mapping is incremented by zero on every such fill, so `newConsumed` never approaches `maxAssets`, and the cap check `require(newConsumed <= offer.maxAssets)` is trivially satisfied forever. The maker accumulates unbounded credit beyond their stated limit while `consumed` permanently reads `0`.

## Finding Description

**Exact code path** — `src/Midnight.sol` lines 358–369:

`tickToPrice` is confirmed to return exactly `WAD` only at `MAX_TICK` and strictly less than `WAD` for every other tick: [1](#0-0) [2](#0-1) 

For a buy offer, `buyerPrice = offerPrice` (the `_settlementFee` cancels out): [3](#0-2) 

For `units = 1` and any tick below `MAX_TICK`, `buyerPrice < WAD`, so `mulDivDown(1, buyerPrice, WAD) = floor(buyerPrice / WAD) = 0`. The `consumed` mapping is then incremented by zero: [4](#0-3) 

`require(0 <= offer.maxAssets)` is trivially satisfied on every fill.

**Position accounting proceeds normally despite zero assets.** `buyerCreditIncrease = zeroFloorSub(units, buyerPos.debt)` equals `units = 1` when the maker has no debt, so `buyerPos.credit += 1` per fill: [5](#0-4) 

The taker (seller) accumulates `sellerDebtIncrease = 1` per fill. Token transfers at lines 455–456 both move `0` tokens since `buyerAssets = sellerAssets = 0`.

**Why existing checks fail:**
- `require(newConsumed <= offer.maxAssets)` — trivially satisfied since `newConsumed` stays `0`.
- Certora `takeConsumedBoundedByMax` (Consume.spec line 62) only asserts `consumed <= maxAssets`, which holds vacuously at `0`; it does not assert that `consumed` must increase by a positive amount per non-zero fill.
- `takeConsumedDelta` (Consume.spec line 67–68) is explicitly scoped to `require offer.maxAssets == 0` — it does not cover the assets-mode branch at all.
- `mulDivDown` and `mulDivUp` are replaced with `NONDET` in Consume.spec (lines 11–12), so the prover never witnesses the truncation-to-zero case. [6](#0-5) [7](#0-6) 

## Impact Explanation
The `maxAssets` cap on a buy offer is completely unenforceable whenever `buyerPrice < WAD` — i.e., for every tick except `MAX_TICK`, which is the overwhelming majority of real offers. The `consumed` mapping — the sole on-chain record of fill volume for the offer group — remains at `0` regardless of how many fills occur, violating the core invariant that `consumed` monotonically tracks fill volume and that offers cannot be overfilled. The maker accumulates unbounded credit beyond their stated limit. Token transfers both move `0` tokens, so the taker pays nothing and receives nothing in loan tokens, but takes on `1` unit of debt per fill (requiring collateral), making this a griefing vector that fully bypasses the maker's cap at the taker's own collateral cost.

## Likelihood Explanation
- **Preconditions:** Any buy offer with `maxAssets > 0` at any tick except `MAX_TICK` — the overwhelming majority of real offers, since `MAX_TICK` corresponds to price = 1 WAD (par value).
- **Feasibility:** The taker only needs to call `take` with `units = 1` repeatedly. No special privileges, no oracle manipulation, no flash loan required. The only cost to the attacker is collateral to back the debt taken on per fill.
- **Repeatability:** Unlimited; the cap is never consumed.

## Recommendation
Track fill volume in units rather than (or in addition to) assets when `maxAssets > 0`, or enforce a minimum `buyerAssets > 0` when `units > 0` and `maxAssets > 0`. Concretely, one fix is to require `buyerAssets > 0` whenever `units > 0` and `offer.maxAssets > 0`, preventing fills that produce zero asset accounting. Alternatively, convert `maxAssets` to a units-equivalent threshold at the offer's tick before comparing, ensuring the cap is always expressed in the same granularity as the fill. The Certora `Consume.spec` should also replace the `NONDET` summaries for `mulDivDown`/`mulDivUp` with their concrete implementations (or add a `takeConsumedDelta` variant for the assets-mode branch) so the prover can witness the truncation case.

## Proof of Concept
```solidity
// Foundry test sketch
function testMaxAssetsBypassWithUnitFill() public {
    // 1. Maker posts a buy offer at tick = MAX_TICK - 2 (price = 1e18 - 1e12 < WAD)
    //    with maxAssets = 1e18 (1 WAD), maxUnits = 0
    Offer memory offer = Offer({
        buy: true,
        tick: MAX_TICK - 2,
        maxAssets: 1e18,
        maxUnits: 0,
        group: bytes32(0),
        // ... other fields
    });

    // 2. Taker calls take with units = 1 repeatedly
    for (uint i = 0; i < 1000; i++) {
        midnight.take(offer, ratifierData, 1, taker, receiver, address(0), "");
    }

    // 3. Assert: consumed is still 0, maker has 1000 units of credit
    assertEq(midnight.consumed(maker, bytes32(0)), 0);
    assertEq(midnight.position(id, maker).credit, 1000);
    // maxAssets cap was never triggered despite 1000 fills
}
```

### Citations

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

**File:** test/TickLibTest.sol (L15-19)
```text
    function testTickToPriceMinMax() public pure {
        assertEq(TickLib.tickToPrice(0), 0, "tick 0");
        assertEq(TickLib.tickToPrice(2), 1e12, "first non-zero tick");
        assertEq(TickLib.tickToPrice(MAX_TICK - 2), 1e18 - 1e12, "tick max - 2 just below par");
        assertEq(TickLib.tickToPrice(MAX_TICK), 1e18, "tick max");
```

**File:** src/Midnight.sol (L361-364)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/Midnight.sol (L366-369)
```text
        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L382-410)
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
```

**File:** certora/specs/Consume.spec (L11-12)
```text
    function UtilsLib.mulDivDown(uint256, uint256, uint256) internal returns (uint256) => NONDET;
    function UtilsLib.mulDivUp(uint256, uint256, uint256) internal returns (uint256) => NONDET;
```

**File:** certora/specs/Consume.spec (L58-74)
```text
/// After a successful take, consumed[offer.maker][offer.group] does not exceed the effective max.
rule takeConsumedBoundedByMax(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert offer.maxAssets > 0 => consumed(offer.maker, offer.group) <= offer.maxAssets;
    assert offer.maxAssets == 0 => consumed(offer.maker, offer.group) <= offer.maxUnits;
}

/// After a successful take in units mode, the change in consumed equals the units taken.
rule takeConsumedDelta(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxAssets == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert consumed(offer.maker, offer.group) == consumedBefore + units;
```
