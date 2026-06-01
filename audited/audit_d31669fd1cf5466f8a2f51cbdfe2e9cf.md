### Title
`buyerAssetsToUnits` reverts for sell offers with `offerPrice + settlementFee > WAD` while `take()` succeeds, causing `buyWithAssetsTargetAndWithdrawCollateral` to revert entirely instead of skipping - (`File: src/periphery/TakeAmountsLib.sol`)

### Summary

For sell offers (`offer.buy = false`), `Midnight.take()` imposes no upper-bound check on `buyerPrice`, so it succeeds even when `offerPrice + settlementFee > WAD`. `TakeAmountsLib.buyerAssetsToUnits` applies a strict `require(buyerPrice <= WAD)` guard that `take()` does not share. Because `buyerAssetsToUnits` is called **outside** the try/catch in `buyWithAssetsTargetAndWithdrawCollateral`, any sell offer at a high-price tick with a non-zero settlement fee causes the entire bundler call to revert rather than skipping the offer.

### Finding Description

**Code path and root cause:**

In `TakeAmountsLib.buyerAssetsToUnits` (lines 26–28), for a sell offer:

```
sellerPrice = offerPrice          // offer.buy == false branch
buyerPrice  = offerPrice + settlementFee
require(buyerPrice <= WAD, PriceGreaterThanOne())   // ← REVERTS
``` [1](#0-0) 

In `Midnight.take()` (lines 361–364), the identical arithmetic is performed for a sell offer, but **there is no analogous `require`**:

```
sellerPrice = offerPrice
buyerPrice  = offerPrice + settlementFee   // may exceed WAD — no check
buyerAssets = units.mulDivUp(buyerPrice, WAD)   // succeeds
``` [2](#0-1) 

The `take()` NatSpec explicitly acknowledges this asymmetry: "All sellerAssets are reachable with the units input, and all buyerAssets are reachable only if buyerPrice <= WAD." This means `take()` is intentionally permissive for sell offers with `buyerPrice > WAD`. [3](#0-2) 

In `buyWithAssetsTargetAndWithdrawCollateral`, the call to `TakeAmountsLib.buyerAssetsToUnits` is at line 209, **outside** the try/catch that wraps `take()` at lines 215–221:

```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.buyerAssetsToUnits(   // ← NOT in try/catch; reverts propagate
        MIDNIGHT, id, takes[i].offer, targetFilledBuyerAssets - filledBuyerAssets
    ),
    ...
);
try IMidnight(MIDNIGHT).take(...) {      // ← only this is caught
    ...
} catch {}
``` [4](#0-3) 

The bundler's own NatSpec at line 175 documents this: "Reverts if TakeAmountsLib or ConsumableUnitsLib reverts." But line 174 also claims "Skips every reason why take can revert." These two statements are contradictory for sell offers where `buyerPrice > WAD`, because `take()` would **not** revert while `buyerAssetsToUnits` **does**. [5](#0-4) 

**Attacker-controlled inputs:**

- `offer.tick`: the maker sets this freely; no on-chain validation prevents a tick where `tickToPrice(tick)` is close to WAD.
- `settlementFee`: non-zero by market configuration; can also increase post-offer-creation via admin action, retroactively pushing a previously-valid offer into the `buyerPrice > WAD` region.

**Exploit flow:**

1. Maker (attacker or honest) creates a sell offer with `offer.tick` such that `tickToPrice(tick)` is near WAD (e.g., `tick = MAX_TICK`).
2. Market has a non-zero settlement fee, so `offerPrice + settlementFee > WAD`.
3. Taker calls `buyWithAssetsTargetAndWithdrawCollateral` with this offer in the `takes` array.
4. At line 209, `buyerAssetsToUnits` reverts with `PriceGreaterThanOne`.
5. The revert propagates out of the loop — the entire transaction reverts, including any collateral withdrawals and all other offers in the bundle.
6. Calling `Midnight.take()` directly on the same offer with the same parameters succeeds.

**Why existing checks fail:**

The try/catch at lines 215–221 only protects the `take()` call itself. The pre-computation of `unitsToTake` via `buyerAssetsToUnits` is unguarded. There is no pre-flight check in the bundler that `offerPrice + settlementFee <= WAD` before invoking `buyerAssetsToUnits`.

### Impact Explanation

`buyWithAssetsTargetAndWithdrawCollateral` reverts entirely — not just skips the problematic offer — whenever any offer in the `takes` array has `offerPrice + settlementFee > WAD`. This breaks the bundler's documented skip-on-revert guarantee for that offer class, prevents the taker from completing the bundle (including collateral withdrawals), and can be triggered by a malicious maker who places a sell offer at a high-price tick in a market with any non-zero settlement fee.

### Likelihood Explanation

**Preconditions:**
- A sell offer exists with `tickToPrice(offer.tick) + settlementFee > WAD`.
- The offer is included in a `buyWithAssetsTargetAndWithdrawCollateral` call.

**Feasibility:** Any maker can set `offer.tick = MAX_TICK`. Any non-zero settlement fee (which is the normal operating state of the protocol) satisfies the second condition. The condition can also arise retroactively if the settlement fee is increased after an offer is created at a high tick. No special privilege is required beyond being a maker.

**Repeatability:** Deterministic and repeatable for any offer satisfying the precondition.

### Recommendation

Wrap the `buyerAssetsToUnits` (and `ConsumableUnitsLib.consumableUnits`) calls in a try/catch, or add a pre-flight guard that skips the offer when `buyerPrice > WAD`, consistent with the bundler's documented skip-on-revert behavior:

```solidity
// Option A: skip the offer if buyerPrice > WAD
uint256 offerPrice = TickLib.tickToPrice(takes[i].offer.tick);
uint256 fee = IMidnight(MIDNIGHT).settlementFee(id, ...);
if (offerPrice + fee > WAD) continue;

uint256 unitsToTake = min(
    TakeAmountsLib.buyerAssetsToUnits(...),
    ...
);
```

Alternatively, `buyerAssetsToUnits` could return 0 instead of reverting when `buyerPrice > WAD`, and the bundler would naturally skip the offer via the `min(0, ...)` result.

### Proof of Concept

```solidity
// Foundry unit test
function testBuyerAssetsToUnitsRevertsButTakeSucceeds() public {
    // Setup: market with non-zero settlement fee
    uint256 fee = 1e15; // 0.1% — any non-zero value
    midnight.setDefaultSettlementFee(address(loanToken), 1, fee);
    bytes32 id = midnight.touchMarket(market);
    midnight.setMarketTickSpacing(id, 1);

    // Sell offer at MAX_TICK: tickToPrice(MAX_TICK) is close to WAD
    // Ensure tickToPrice(MAX_TICK) + fee > WAD
    uint256 maxTick = ...; // highest tick where tickToPrice > WAD - fee
    offer.buy = false;
    offer.tick = maxTick;
    offer.maker = borrower;
    offer.receiverIfMakerIsSeller = borrower;

    uint256 offerPrice = TickLib.tickToPrice(maxTick);
    assertGt(offerPrice + fee, WAD, "precondition: buyerPrice > WAD");

    // Assert: buyerAssetsToUnits reverts
    vm.expectRevert(TickLib.PriceGreaterThanOne.selector);
    TakeAmountsLib.buyerAssetsToUnits(address(midnight), id, offer, 1e18);

    // Assert: take() itself does NOT revert
    collateralize(market, borrower, 1);
    vm.prank(lender);
    (uint256 buyerAssets, uint256 sellerAssets) = midnight.take(
        offer, "", 1, lender, address(0), address(0), ""
    );
    assertGt(buyerAssets, 0, "take succeeded");

    // Assert: buyWithAssetsTargetAndWithdrawCollateral reverts entirely
    Take[] memory takes = new Take[](1);
    takes[0] = Take({offer: offer, ratifierData: "", units: 1});
    vm.expectRevert(TickLib.PriceGreaterThanOne.selector);
    bundles.buyWithAssetsTargetAndWithdrawCollateral(
        1e18, 0, lender, emptyPermit, takes, new CollateralWithdrawal[](0), lender, 0, address(0)
    );
}
```

Expected assertions:
- `buyerAssetsToUnits` reverts with `PriceGreaterThanOne`.
- `midnight.take()` with the same offer and `units = 1` returns without reverting.
- `buyWithAssetsTargetAndWithdrawCollateral` reverts (propagated from `buyerAssetsToUnits`, not from the try/catch around `take()`).

### Citations

**File:** src/periphery/TakeAmountsLib.sol (L26-28)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + settlementFee;
        require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne());
```

**File:** src/Midnight.sol (L333-334)
```text
    /// @dev All sellerAssets are reachable with the units input, and all buyerAssets are reachable only if buyerPrice
    /// <= WAD.
```

**File:** src/Midnight.sol (L361-364)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/periphery/MidnightBundles.sol (L174-176)
```text
    /// @dev Skips every reason why take can revert (including ones that are not asynchrony related).
    /// @dev Reverts if TakeAmountsLib or ConsumableUnitsLib reverts.
    /// @dev If taking an offer reverts, the bundler will completely skip this offer.
```

**File:** src/periphery/MidnightBundles.sol (L208-221)
```text
            uint256 unitsToTake = min(
                TakeAmountsLib.buyerAssetsToUnits(
                    MIDNIGHT, id, takes[i].offer, targetFilledBuyerAssets - filledBuyerAssets
                ),
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
            try IMidnight(MIDNIGHT)
                .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (
                uint256 resBuyerAssets, uint256
            ) {
                filledUnits += unitsToTake;
                filledBuyerAssets += resBuyerAssets;
            } catch {}
```
