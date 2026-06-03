Audit Report

## Title
`mulDivUp` Arithmetic Overflow in `consumableUnits` for Buy Offers with Oversized `maxAssets` - (File: `src/periphery/ConsumableUnitsLib.sol`)

## Summary
When `consumableUnits` is called for a buy offer with `maxUnits == 0` and `maxAssets > type(uint256).max / WAD` (~1.157e59), the intermediate product `targetBuyerAssets * WAD` in `UtilsLib.mulDivUp` overflows under Solidity 0.8.x checked arithmetic, causing an unconditional revert. Because `supplyCollateralAndSellWithUnitsTarget` and `supplyCollateralAndSellWithAssetsTarget` invoke `ConsumableUnitsLib.consumableUnits` outside their `try/catch` blocks, a single crafted offer in the `takes` array permanently DoS-es the entire bundle call for any caller that includes it.

## Finding Description

**Step 1 — `ConsumableUnitsLib.consumableUnits`:**

With `offer.maxUnits == 0` and `offer.buy == true`, the function enters the `else if` branch: [1](#0-0) 

`targetBuyerAssets = offer.maxAssets.zeroFloorSub(consumed)`. With `consumed == 0`, this equals `offer.maxAssets` directly. `zeroFloorSub` is implemented in assembly and only floors at zero — it does not cap the value. [2](#0-1) 

**Step 2 — `TakeAmountsLib.buyerAssetsToUnits`:**

For a buy offer, line 29 executes: [3](#0-2) 

This calls `mulDivUp(targetBuyerAssets, WAD, buyerPrice)`. The `require(buyerPrice <= WAD)` check only bounds `buyerPrice` from above; it places no constraint on `targetBuyerAssets`.

**Step 3 — `UtilsLib.mulDivUp`:** [4](#0-3) 

There is no `unchecked` block. Under Solidity `^0.8.0`, `x * y` reverts on overflow. With `y = WAD = 1e18`, any `targetBuyerAssets > type(uint256).max / 1e18` (~1.157e59) causes an unconditional overflow revert. `offer.maxAssets` is a plain `uint256` with no protocol-enforced upper bound. [5](#0-4) 

**Step 4 — Propagation to `MidnightBundles`:**

In `supplyCollateralAndSellWithUnitsTarget`, `consumableUnits` is called at line 150, **before** the `try` block at line 152: [6](#0-5) 

In `supplyCollateralAndSellWithAssetsTarget`, the same pattern appears at line 290, before the `try` block at line 292: [7](#0-6) 

The contract's own NatSpec explicitly documents this: *"Reverts if ConsumableUnitsLib reverts."* [8](#0-7) 

Both functions require `takes[i].offer.buy == true`, which is exactly the offer type that triggers the vulnerable branch in `consumableUnits`. [9](#0-8) [10](#0-9) 

## Impact Explanation
Any call to `supplyCollateralAndSellWithUnitsTarget` or `supplyCollateralAndSellWithAssetsTarget` that includes a buy offer with `maxUnits == 0` and `maxAssets > type(uint256).max / 1e18` reverts unconditionally. The entire bundle transaction fails — including any legitimate offers earlier in the `takes` array that would otherwise succeed. Off-chain routing systems and aggregators that call `consumableUnits` to compute fill amounts are similarly broken for the affected offer class. The core `Midnight.take` is unaffected.

## Likelihood Explanation
A maker needs only to sign an offer with `buy = true`, `maxUnits = 0`, and `maxAssets = type(uint256).max`. No special privilege, no oracle manipulation, and no existing state is required. The condition is trivially reachable on first call with zero prior consumption. Any routing system or aggregator that automatically ingests on-chain or off-chain offers and passes them into bundle calls without validating `maxAssets` magnitude can be fed this poisoned offer, causing bundle-level reverts for legitimate takers.

## Recommendation
Use a safe muldiv implementation that avoids intermediate overflow, such as Solady's `FullMath.mulDivUp` (using 512-bit intermediate arithmetic), or add an explicit cap/check on `targetBuyerAssets` before calling `mulDivUp`. The minimal targeted fix is to guard `mulDivUp` calls in `TakeAmountsLib.buyerAssetsToUnits` and `sellerAssetsToUnits`:

```solidity
require(targetBuyerAssets <= type(uint256).max / WAD, OverflowError());
return targetBuyerAssets.mulDivUp(WAD, buyerPrice);
```

Alternatively, replace `UtilsLib.mulDivUp` with a 512-bit overflow-safe implementation throughout the periphery library.

## Proof of Concept
```solidity
// Minimal Foundry test (pseudo-code)
Offer memory poisoned = Offer({
    buy: true,
    maxUnits: 0,
    maxAssets: type(uint256).max,
    // ... other fields set to valid values
});
Take[] memory takes = new Take[](1);
takes[0] = Take({ offer: poisoned, units: 1e18, ratifierData: "" });

// This call reverts with arithmetic overflow, not OutOfOffers
vm.expectRevert(); // arithmetic overflow
bundles.supplyCollateralAndSellWithUnitsTarget(
    1e18, 0, taker, receiver, new CollateralSupply[](0), takes, 0, address(0)
);
```

The revert occurs inside `consumableUnits` → `buyerAssetsToUnits` → `mulDivUp` at the `x * y` multiplication, before the `try IMidnight(...).take(...)` block is ever reached.

### Citations

**File:** src/periphery/ConsumableUnitsLib.sol (L18-19)
```text
        } else if (offer.buy) {
            return TakeAmountsLib.buyerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
```

**File:** src/libraries/UtilsLib.sol (L22-26)
```text
    function zeroFloorSub(uint256 x, uint256 y) internal pure returns (uint256 z) {
        assembly {
            z := mul(gt(x, y), sub(x, y))
        }
    }
```

**File:** src/libraries/UtilsLib.sol (L33-36)
```text
    /// @dev Returns (x * y) / d rounded up.
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
```

**File:** src/periphery/TakeAmountsLib.sol (L28-29)
```text
        require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne());
        return offer.buy ? targetBuyerAssets.mulDivUp(WAD, buyerPrice) : targetBuyerAssets.mulDivDown(WAD, buyerPrice);
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

**File:** src/periphery/MidnightBundles.sol (L145-145)
```text
            require(takes[i].offer.buy, InconsistentSide());
```

**File:** src/periphery/MidnightBundles.sol (L147-152)
```text
            uint256 unitsToTake = min(
                targetUnits - filledUnits,
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
            try IMidnight(MIDNIGHT)
```

**File:** src/periphery/MidnightBundles.sol (L283-283)
```text
            require(takes[i].offer.buy, InconsistentSide());
```

**File:** src/periphery/MidnightBundles.sol (L285-292)
```text
            uint256 unitsToTake = min(
                TakeAmountsLib.sellerAssetsToUnits(
                    MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
                ),
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
            try IMidnight(MIDNIGHT)
```
