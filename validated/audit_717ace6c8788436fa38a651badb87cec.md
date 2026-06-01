I need to verify the core mathematical claims before rendering a verdict — specifically whether `tickToPrice` always returns a value strictly less than `WAD`, and whether the `mulDivDown(1, offerPrice, WAD)` rounding truly produces zero for all valid ticks.

Audit Report

## Title
Zero Settlement Fee and Consumed Counter Stall on Unit-Granularity Buy-Offer Takes Due to Integer Rounding — (`src/Midnight.sol`)

## Summary
When `offer.buy == true` and `offer.maxAssets > 0`, a taker can call `take` with `units = 1` on any buy offer whose tick price is strictly less than `WAD` (1e18). Because `buyerAssets` is computed with `mulDivDown(1, offerPrice, WAD)` and `offerPrice < WAD`, the result rounds to zero. Consequently, the `consumed` counter never advances (it increments by `buyerAssets = 0`), the `maxAssets` cap is never reached, and `claimableSettlementFee` receives zero despite real credit and debt being created. The offer can be taken an unlimited number of times at negligible cost on cheap-gas chains.

## Finding Description

**Root cause — `src/Midnight.sol` lines 363–364, 368, 418:**

```solidity
// line 363-364
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

// line 367-369 (maxAssets branch)
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}

// line 418
claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

For `offer.buy == true`, `units = 1`, and any tick where `offerPrice < WAD`:
- `buyerAssets = mulDivDown(1, offerPrice, WAD) = 0` (since `1 * offerPrice < WAD`)
- `sellerAssets = mulDivDown(1, sellerPrice, WAD) = 0` (since `sellerPrice < offerPrice < WAD`)
- `consumed[maker][group] += 0` → counter unchanged → `require(0 <= maxAssets)` always passes
- `claimableSettlementFee += 0 - 0 = 0`

Meanwhile, position accounting uses `units` directly (not `buyerAssets`):
```solidity
// lines 382-384
uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt); // = 1 for fresh buyer
uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);       // = 0 for fresh seller
uint256 sellerDebtIncrease = units - sellerCreditDecrease;                  // = 1
```

So `buyerPos.credit += 1` and `sellerPos.debt += 1` execute with zero tokens transferred and zero fee collected.

**Tick range affected:** `tickToPrice` returns values in `[0, WAD]`. The formal Certora spec (`certora/specs/TickToPrice.spec` line 38–39) and the unit test (`test/TickLibTest.sol` line 19) confirm `tickToPrice(MAX_TICK) == 1e18 == WAD` exactly. Therefore the zero-rounding condition (`offerPrice < WAD`) holds for all ticks **0 through MAX_TICK − 1** (i.e., ticks 0–5819), covering essentially the entire usable tick range. The claim's assertion that MAX_TICK itself is below WAD is incorrect — `tickToPrice(5820) = WAD` — but this does not affect the validity of the finding since the vulnerable range is still nearly the entire tick space.

**Why existing checks fail:**
- No `require(buyerAssets > 0)` guard exists before the consumed update or fee accrual.
- The `ConsumedAssets` check (`require(newConsumed <= offer.maxAssets)`) passes trivially because `newConsumed` never advances.
- The `sellerPrice = offerPrice - _settlementFee` underflow guard only prevents `offerPrice < _settlementFee`; it does not prevent `offerPrice < WAD`.
- The code comment at line 113–114 acknowledges "fees manipulations on chains with very cheap gas" but provides no on-chain enforcement. The comment at line 94 acknowledges "It is possible to give units to a fully consumed assets-based buy offer with price < 1," confirming the behavior is reachable but not that it is safe.

## Impact Explanation
For every `take` with `units = 1` on a buy offer at any sub-WAD tick price:
1. **Settlement fee is never collected** despite real credit and debt being created. The invariant "settlement fee must increase for every non-zero-units take with non-zero settlement fee" is violated.
2. **The `consumed` counter stalls at zero**, meaning the `maxAssets` cap is permanently bypassed. The offer can be taken an unlimited number of times.
3. **Credit and debt are created with zero token transfer.** A colluding maker/taker pair (taker must hold sufficient collateral to pass the `isHealthy` check at line 476) can accumulate unbounded credit for the maker at negligible cost. On L2s where base transaction cost is a few cents, the protocol's entire expected settlement fee revenue stream across any number of takes can be drained.

## Likelihood Explanation
All preconditions are reachable by any unprivileged user:
1. Market has a non-zero settlement fee — the normal operating condition.
2. `offer.buy = true`, `offer.maxAssets > 0` — standard buy-offer configuration.
3. `offer.tick` is any value in `[0, 5819]` — covers essentially the entire valid tick range.
4. Taker passes `units = 1` and holds enough collateral to remain healthy.

The attack is repeatable an unlimited number of times on the same offer since `consumed` never advances. On cheap-gas chains (Optimism, Base, Arbitrum) the cost per zero-fee take is only the base transaction cost.

## Recommendation
Add a guard that reverts (or skips the take) when `buyerAssets == 0` and `units > 0` for buy offers, or equivalently enforce a minimum `units` such that `mulDivDown(units, offerPrice, WAD) > 0`:

```solidity
// After computing buyerAssets / sellerAssets:
if (offer.buy) {
    require(buyerAssets > 0 || units == 0, ZeroAssets());
} else {
    require(sellerAssets > 0 || units == 0, ZeroAssets());
}
```

Alternatively, enforce a minimum `units` input: `require(units == 0 || units >= WAD / offerPrice + 1)`. This ensures at least 1 asset is transferred per take, preserving the fee and consumed-counter invariants.

## Proof of Concept
Minimal Foundry test outline:

```solidity
function testZeroFeeUnlimitedTakes() public {
    // Setup: market with non-zero settlement fee, buy offer at tick < MAX_TICK
    offer.buy = true;
    offer.maxAssets = 1e18; // any positive cap
    offer.tick = 5816;      // tickToPrice(5816) < WAD → buyerAssets = 0 for units=1

    // Taker supplies collateral to remain healthy
    collateralize(market, taker, 1000);

    uint256 feeBefore = midnight.claimableSettlementFee(market.loanToken);
    uint256 consumedBefore = midnight.consumed(offer.maker, offer.group);

    // Take 1: units = 1
    vm.prank(taker);
    midnight.take(offer, hex"", 1, taker, taker, address(0), hex"");

    // Take 2: same offer, same units — should revert if maxAssets cap worked, but it doesn't
    vm.prank(taker);
    midnight.take(offer, hex"", 1, taker, taker, address(0), hex"");

    // Assertions
    assertEq(midnight.claimableSettlementFee(market.loanToken), feeBefore, "no fee collected");
    assertEq(midnight.consumed(offer.maker, offer.group), consumedBefore, "consumed unchanged");
    assertEq(midnight.creditOf(id, offer.maker), 2, "maker got 2 units credit for free");
    assertEq(midnight.debtOf(id, taker), 2, "taker got 2 units debt, paid 0 tokens");
}
```

Expected result: both takes succeed, `claimableSettlementFee` is unchanged, `consumed` stays at 0, and the maker accumulates credit without any token transfer. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** src/Midnight.sol (L93-94)
```text
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L112-114)
```text
/// ROUNDINGS
/// @dev assets are rounded against the taker and in favor of the maker in take. Therefore, the settlement fee has no
/// defined rounding direction, which could lead to fees manipulations on chains with very cheap gas.
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

**File:** src/Midnight.sol (L418-418)
```text
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
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

**File:** test/TickLibTest.sol (L15-20)
```text
    function testTickToPriceMinMax() public pure {
        assertEq(TickLib.tickToPrice(0), 0, "tick 0");
        assertEq(TickLib.tickToPrice(2), 1e12, "first non-zero tick");
        assertEq(TickLib.tickToPrice(MAX_TICK - 2), 1e18 - 1e12, "tick max - 2 just below par");
        assertEq(TickLib.tickToPrice(MAX_TICK), 1e18, "tick max");
    }
```

**File:** certora/specs/TickToPrice.spec (L38-39)
```text
rule tickToPriceIsOneAtMaxTick() {
    assert tickToPrice(maxTick()) == 10 ^ 18;
```
