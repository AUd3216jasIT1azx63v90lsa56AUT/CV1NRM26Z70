### Title
`sellerAssetsToUnits` reverts on zero `sellerPrice` while `Midnight.take()` succeeds, blocking valid bundler fills at the settlement-fee price boundary - (File: src/periphery/TakeAmountsLib.sol)

### Summary
When `offer.buy = true` and `tickToPrice(offer.tick)` exactly equals `settlementFee(id, ttm)`, `sellerPrice` computes to zero. `Midnight.take()` handles this correctly — `sellerAssets = units.mulDivDown(0, WAD) = 0` — but `sellerAssetsToUnits` calls `targetSellerAssets.mulDivUp(WAD, 0)`, which triggers an arithmetic underflow in `mulDivUp`'s `(d - 1)` term when `d = 0`, causing an unconditional revert. Because `supplyCollateralAndSellWithAssetsTarget` calls `sellerAssetsToUnits` outside its try/catch block, the entire bundler call reverts while a direct `Midnight.take()` with the same inputs would succeed.

### Finding Description

**Root cause — `mulDivUp` with denominator zero:**

`UtilsLib.mulDivUp` is implemented as:

```solidity
// src/libraries/UtilsLib.sol:34-36
function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y + (d - 1)) / d;
}
```

When `d = 0`, the sub-expression `d - 1` underflows under Solidity 0.8 checked arithmetic, reverting with `stdError.arithmeticError` regardless of `x`. This is confirmed by the existing test:

```solidity
// test/UtilsLibTest.sol:80-84
function testMulDivUpDivisionByZero(uint256 x, uint256 y) public {
    // because there is d-1.
    vm.expectRevert(stdError.arithmeticError);
    this.mulDivUp(x, y, 0);
}
```

**Code path in `sellerAssetsToUnits`:** [1](#0-0) 

```solidity
uint256 offerPrice = TickLib.tickToPrice(offer.tick);
uint256 settlementFee =
    IMidnight(midnight).settlementFee(id, UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp));
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...;
```

When `offer.buy = true` and `offerPrice == settlementFee`, `sellerPrice = 0`, and `mulDivUp(WAD, 0)` reverts.

**Contrast with `Midnight.take()`:** [2](#0-1) 

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
// ...
uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : ...;
```

Here `sellerAssets = units.mulDivDown(0, WAD) = (units * 0) / WAD = 0`. The denominator is `WAD` (never zero), so `Midnight.take()` succeeds and returns `sellerAssets = 0`.

**Call site in the bundler — outside try/catch:** [3](#0-2) 

```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(          // ← reverts here, propagates up
        MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
    ),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
);
try IMidnight(MIDNIGHT).take(...) returns (...) { ... } catch {}  // ← never reached
```

`sellerAssetsToUnits` is called before the `try/catch`, so its revert propagates to the caller unconditionally.

**The comment in `sellerAssetsToUnits` is also wrong:** [4](#0-3) 

The NatSpec says "Reverts if offerPrice < settlementFee in case of a buy offer (midnight reverts too)." But at the boundary `offerPrice == settlementFee`, `Midnight.take()` does **not** revert — only the periphery helper does.

**Preconditions for the trigger:**
- `offer.buy = true`
- `tickToPrice(offer.tick) == settlementFee(id, ttm)` at the moment of the call
- `targetSellerAssets > 0` (or `referralFeePct > 0` making `targetFilledSellerAssets > 0`)

The settlement fee at `ttm = 0` is `settlementFeeCbp0 * CBP` where `CBP = 1e12`. The fee setter can set this to any multiple of `1e12` up to `maxSettlementFee(0)` (~1.4e15). Tick prices at very low ticks fall in this range. If the fee setter sets `settlementFeeCbp0 * CBP` to a value that equals `tickToPrice(someTick)`, any taker calling `supplyCollateralAndSellWithAssetsTarget` against a buy offer at that tick (at or near maturity) is blocked.

### Impact Explanation

Any taker calling `supplyCollateralAndSellWithAssetsTarget` against a buy offer where `tickToPrice(offer.tick) == settlementFee(id, ttm)` receives an unconditional revert from the bundler. The taker cannot execute the bundle fill through the periphery. Direct `Midnight.take()` with the same `units` would succeed and return `sellerAssets = 0`. The taker's collateral supply (already executed earlier in the function) is committed before the revert, but since the revert propagates the entire transaction is rolled back — so no funds are lost, but the fill is completely blocked through the bundler path.

### Likelihood Explanation

The condition requires `tickToPrice(offer.tick) == settlementFee(id, ttm)` exactly. Settlement fees are multiples of `CBP = 1e12`; tick prices are irrational-looking values from `1e36 / (1e18 + wExp(...))`. Exact equality requires a tick price that is a multiple of `1e12`, which is possible but not guaranteed for arbitrary ticks. The most accessible trigger is at `ttm = 0` (maturity boundary) where the fee is the constant `settlementFeeCbp0 * CBP`. If the fee setter configures this value to match a specific tick price (even inadvertently), the condition is met for the entire post-maturity window. The scenario is low-probability but non-zero and repeatable once the fee/tick alignment exists.

### Recommendation

Add a guard in `sellerAssetsToUnits` for the `sellerPrice == 0` case, mirroring the semantics of `Midnight.take()`:

```solidity
function sellerAssetsToUnits(...) internal view returns (uint256) {
    uint256 offerPrice = TickLib.tickToPrice(offer.tick);
    uint256 settlementFee = IMidnight(midnight).settlementFee(
        id, UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp)
    );
    uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
    // When sellerPrice == 0, sellerAssets == 0 for any units; any units value satisfies
    // the target of 0, but targetSellerAssets > 0 is unreachable — revert explicitly.
    if (offer.buy && sellerPrice == 0) {
        require(targetSellerAssets == 0, "sellerPrice is zero: target unreachable");
        return type(uint256).max; // or 0, depending on desired semantics
    }
    return offer.buy
        ? targetSellerAssets.mulDivUp(WAD, sellerPrice)
        : targetSellerAssets.mulDivDown(WAD, sellerPrice);
}
```

Alternatively, update the NatSpec to document that `sellerPrice == 0` also causes a revert, and add a pre-check in `supplyCollateralAndSellWithAssetsTarget` to skip offers where `sellerPrice == 0` (treating them as zero-yield and skipping rather than reverting).

### Proof of Concept

```solidity
// Foundry unit test
function testSellerAssetsToUnitsZeroSellerPriceRevert() public {
    // 1. Find a tick whose price equals a valid fee value (multiple of 1e12).
    //    Use tick=896 (small price) as a concrete example; adjust to match.
    uint256 tick = 896;
    uint256 offerPrice = TickLib.tickToPrice(tick);
    // Round offerPrice down to nearest 1e12 multiple for the fee.
    uint256 feeValue = (offerPrice / 1e12) * 1e12;
    vm.assume(feeValue > 0 && feeValue == offerPrice); // exact match required

    // 2. Set settlementFeeCbp0 so that settlementFee(id, 0) == offerPrice.
    midnight.setMarketSettlementFee(id, 0, feeValue);

    // 3. Warp to exactly maturity so ttm=0 and fee = settlementFeeCbp0 * CBP.
    vm.warp(market.maturity);

    // 4. Construct a buy offer at that tick.
    Offer memory buyOffer = ...; // offer.buy = true, offer.tick = tick

    // 5. Assert sellerAssetsToUnits reverts.
    vm.expectRevert(); // arithmetic underflow in mulDivUp(WAD, 0)
    TakeAmountsLib.sellerAssetsToUnits(address(midnight), id, buyOffer, 1e18);

    // 6. Assert direct Midnight.take() with units=1 succeeds and returns sellerAssets=0.
    collateralize(market, taker, 1);
    (uint256 buyerAssets, uint256 sellerAssets) = midnight.take(
        buyOffer, hex"", 1, taker, taker, address(0), ""
    );
    assertEq(sellerAssets, 0, "direct take returns sellerAssets=0");

    // 7. Assert supplyCollateralAndSellWithAssetsTarget reverts.
    Take[] memory takes = new Take[](1);
    takes[0] = Take({offer: buyOffer, units: type(uint256).max, ratifierData: hex""});
    vm.expectRevert();
    midnightBundles.supplyCollateralAndSellWithAssetsTarget(
        1e18, type(uint256).max, taker, taker, new CollateralSupply[](0), takes, 0, address(0)
    );
}
```

**Expected assertions:**
- `sellerAssetsToUnits` reverts with arithmetic error
- `Midnight.take(units=1)` succeeds, `sellerAssets == 0`
- `supplyCollateralAndSellWithAssetsTarget` reverts

### Citations

**File:** src/periphery/TakeAmountsLib.sol (L34-35)
```text
    /// @dev Reverts if offerPrice < settlementFee in case of a buy offer (midnight reverts too).
    /// @dev Returns a number of units for which take yields exactly targetSellerAssets (not necessarily the smallest).
```

**File:** src/periphery/TakeAmountsLib.sol (L41-46)
```text
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
        uint256 settlementFee =
            IMidnight(midnight).settlementFee(id, UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp));
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        return
            offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

**File:** src/Midnight.sol (L361-364)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/periphery/MidnightBundles.sol (L285-300)
```text
            uint256 unitsToTake = min(
                TakeAmountsLib.sellerAssetsToUnits(
                    MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
                ),
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
            try IMidnight(MIDNIGHT)
                .take(
                    takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(this), address(0), ""
                ) returns (
                uint256, uint256 resSellerAssets
            ) {
                filledUnits += unitsToTake;
                filledSellerAssets += resSellerAssets;
            } catch {}
```
