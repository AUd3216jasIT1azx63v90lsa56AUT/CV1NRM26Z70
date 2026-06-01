Audit Report

## Title
`mulDivUp` arithmetic overflow in `buyerAssetsToUnits` DoS-es bundler for buy offers with large `maxAssets` - (File: src/periphery/ConsumableUnitsLib.sol)

## Summary
`ConsumableUnitsLib.consumableUnits` delegates to `TakeAmountsLib.buyerAssetsToUnits` for buy offers when `maxUnits == 0`, which computes `targetBuyerAssets.mulDivUp(WAD, buyerPrice)`. `UtilsLib.mulDivUp` performs `x * y` under Solidity 0.8.x checked arithmetic with no `unchecked` block, causing an arithmetic panic whenever `targetBuyerAssets > type(uint256).max / WAD (~1.157e59)`. Any buy offer with `maxAssets` above this threshold permanently reverts every bundler call that includes it.

## Finding Description

**Code path:**

`ConsumableUnitsLib.consumableUnits` — when `offer.maxUnits == 0` and `offer.buy == true`: [1](#0-0) 

calls `TakeAmountsLib.buyerAssetsToUnits` with `targetBuyerAssets = offer.maxAssets.zeroFloorSub(consumed)`. With `consumed == 0`, `targetBuyerAssets == offer.maxAssets`.

`TakeAmountsLib.buyerAssetsToUnits` for the `offer.buy == true` branch: [2](#0-1) 

`UtilsLib.mulDivUp` — no `unchecked` block, Solidity 0.8.x checked arithmetic: [3](#0-2) 

`WAD = 1e18`. If `targetBuyerAssets > type(uint256).max / 1e18 ≈ 1.157e59`, the intermediate product `x * y` overflows and the EVM reverts with an arithmetic panic.

**Why existing guards fail:**

The only guard in `buyerAssetsToUnits` is `require(buyerPrice <= WAD)`, which constrains the denominator only: [4](#0-3) 

No check in `ConsumableUnitsLib`, `MidnightBundles`, or the `Offer` struct bounds `offer.maxAssets` below the overflow threshold. [5](#0-4) 

**Affected bundler entry points** (both enforce `offer.buy == true` before calling `consumableUnits`, and both propagate the revert per their NatSpec):

- `supplyCollateralAndSellWithUnitsTarget` lines 145 and 150: [6](#0-5) 

- `supplyCollateralAndSellWithAssetsTarget` lines 283 and 290: [7](#0-6) 

Both functions document "Reverts if ConsumableUnitsLib reverts", confirming the revert is not caught. [8](#0-7) 

## Impact Explanation
Any buy offer with `maxUnits == 0` and `maxAssets > type(uint256).max / WAD` causes `supplyCollateralAndSellWithUnitsTarget` and `supplyCollateralAndSellWithAssetsTarget` to revert unconditionally for every taker who includes that offer. Takers lose all bundler convenience (collateral supply, multi-offer routing, slippage checks) and must fall back to calling `Midnight.take` directly with a manually computed `units` value. The core `Midnight.take` is unaffected; the DoS is confined to the periphery bundler.

## Likelihood Explanation
`type(uint256).max` is the idiomatic Solidity sentinel for "no cap on assets". A maker who sets `offer.maxAssets = type(uint256).max` on a buy offer — a natural and common choice — silently breaks all bundler paths for that offer. No privileged access, no oracle manipulation, and no special market state is required. The condition is trivially reproducible: `offer.buy = true`, `offer.maxUnits = 0`, `offer.maxAssets = type(uint256).max`, `consumed = 0`.

## Recommendation
Replace the unchecked multiplication in `UtilsLib.mulDivUp` with a safe full-precision `mulDiv` (e.g., using the 512-bit technique from Uniswap v3's `FullMath`), or add an explicit overflow guard in `buyerAssetsToUnits` before calling `mulDivUp`:

```solidity
require(targetBuyerAssets <= type(uint256).max / WAD, OverflowError());
```

Alternatively, cap `offer.maxAssets` in `consumableUnits` to `type(uint256).max / WAD` before passing it to `buyerAssetsToUnits`, since any value above that threshold is functionally equivalent to "unlimited" given the WAD-denominated price system.

## Proof of Concept
```solidity
// Minimal Foundry test
function test_consumableUnitsOverflow() public {
    Offer memory offer;
    offer.buy = true;
    offer.maxUnits = 0;
    offer.maxAssets = type(uint256).max; // idiomatic "no cap"
    offer.tick = /* any valid tick */;
    offer.market = /* any created market */;

    // This call reverts with arithmetic overflow (panic 0x11)
    ConsumableUnitsLib.consumableUnits(address(midnight), id, offer);
}
```
Steps:
1. Deploy `Midnight` and `MidnightBundles` on a local fork.
2. Create a market and a buy offer with `maxUnits = 0`, `maxAssets = type(uint256).max`.
3. Call `supplyCollateralAndSellWithUnitsTarget` or `supplyCollateralAndSellWithAssetsTarget` with that offer.
4. Observe unconditional revert due to arithmetic overflow in `UtilsLib.mulDivUp`.

### Citations

**File:** src/periphery/ConsumableUnitsLib.sol (L18-19)
```text
        } else if (offer.buy) {
            return TakeAmountsLib.buyerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
```

**File:** src/periphery/TakeAmountsLib.sol (L28-28)
```text
        require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne());
```

**File:** src/periphery/TakeAmountsLib.sol (L29-29)
```text
        return offer.buy ? targetBuyerAssets.mulDivUp(WAD, buyerPrice) : targetBuyerAssets.mulDivDown(WAD, buyerPrice);
```

**File:** src/libraries/UtilsLib.sol (L34-35)
```text
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
```

**File:** src/interfaces/IMidnight.sol (L34-35)
```text
    uint256 maxUnits;
    uint256 maxAssets; // buyerAssets if offer.buy else sellerAssets
```

**File:** src/periphery/MidnightBundles.sol (L111-111)
```text
    /// @dev Reverts if ConsumableUnitsLib reverts.
```

**File:** src/periphery/MidnightBundles.sol (L144-151)
```text
        for (uint256 i; i < takes.length && filledUnits < targetUnits; i++) {
            require(takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
            uint256 unitsToTake = min(
                targetUnits - filledUnits,
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
```

**File:** src/periphery/MidnightBundles.sol (L282-291)
```text
        for (uint256 i; i < takes.length && filledSellerAssets < targetFilledSellerAssets; i++) {
            require(takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
            uint256 unitsToTake = min(
                TakeAmountsLib.sellerAssetsToUnits(
                    MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
                ),
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
```
