Audit Report

## Title
`mulDivUp` Arithmetic Overflow in `consumableUnits` for Buy Offers with Oversized `maxAssets` - (File: `src/periphery/ConsumableUnitsLib.sol`)

## Summary
`UtilsLib.mulDivUp` computes `(x * y + (d - 1)) / d` under checked Solidity 0.8.x arithmetic with no `unchecked` block. When `consumableUnits` is called for a buy offer where `maxUnits == 0` and `maxAssets > type(uint256).max / WAD` (~1.157e59), the intermediate product `targetBuyerAssets * WAD` overflows and the call reverts unconditionally. `MidnightBundles.supplyCollateralAndSellWithUnitsTarget` and `supplyCollateralAndSellWithAssetsTarget` invoke `consumableUnits` outside any `try/catch`, so any bundle containing such a crafted offer reverts entirely.

## Finding Description

**Step 1 — `ConsumableUnitsLib.consumableUnits` (line 18–19):**
With `offer.maxUnits == 0` and `offer.buy == true`, the function enters the `else if` branch:
```solidity
return TakeAmountsLib.buyerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
```
With `consumed == 0`, `targetBuyerAssets = offer.maxAssets` with no upper-bound check. [1](#0-0) 

**Step 2 — `TakeAmountsLib.buyerAssetsToUnits` (line 29):**
For a buy offer, the return statement is:
```solidity
return offer.buy ? targetBuyerAssets.mulDivUp(WAD, buyerPrice) : ...
```
`targetBuyerAssets` is passed directly as `x` to `mulDivUp` with `y = WAD = 1e18`. [2](#0-1) 

**Step 3 — `UtilsLib.mulDivUp` (lines 34–36):**
```solidity
function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y + (d - 1)) / d;
}
```
No `unchecked` block. Under Solidity `^0.8.0`, `x * y` reverts on overflow when `x > type(uint256).max / y`. With `y = 1e18`, any `targetBuyerAssets > ~1.157e59` triggers this revert deterministically. [3](#0-2) 

**Why existing checks fail:**
- `require(buyerPrice <= WAD, ...)` (line 28 of `TakeAmountsLib`) bounds only `buyerPrice`, not `targetBuyerAssets`.
- `zeroFloorSub` floors at zero but imposes no upper cap.
- `atMostOneNonZero` in `Midnight.take` prevents `maxAssets > 0` and `maxUnits > 0` simultaneously but places no magnitude constraint on `maxAssets`. [4](#0-3) 

**Propagation to `MidnightBundles`:**
Both `supplyCollateralAndSellWithUnitsTarget` (line 150) and `supplyCollateralAndSellWithAssetsTarget` (line 290) call `ConsumableUnitsLib.consumableUnits` directly inside the loop, outside any `try/catch`. The `try/catch` wraps only the subsequent `IMidnight(MIDNIGHT).take(...)` call. The NatSpec at lines 111 and 175 explicitly documents: *"Reverts if ConsumableUnitsLib reverts."* [5](#0-4) [6](#0-5) 

## Impact Explanation
Any call to `consumableUnits` for a buy offer where `offer.maxAssets > type(uint256).max / 1e18` reverts unconditionally. This causes `supplyCollateralAndSellWithUnitsTarget` and `supplyCollateralAndSellWithAssetsTarget` in `MidnightBundles` to revert for any bundle that includes such an offer. Off-chain routing systems and aggregators that call `consumableUnits` to compute fill amounts are similarly broken for the affected offer class. The core `Midnight.take` is unaffected. Severity is medium: the DoS is scoped to the periphery bundler and to bundles that include the malicious offer, not to the entire contract or core protocol.

## Likelihood Explanation
A maker needs only to sign an offer with `buy = true`, `maxUnits = 0`, and `maxAssets` set to any value above ~1.157e59 (e.g., `type(uint256).max`). No special privilege, no oracle manipulation, and no existing state is required. The condition is trivially reachable on first call with zero prior consumption. Any routing system or aggregator that automatically ingests on-chain or off-chain offers without validating `maxAssets` magnitude can be fed this poisoned offer, causing bundle-level reverts for legitimate takers.

## Recommendation
Replace the naive `mulDivUp` implementation with an overflow-safe 512-bit multiplication (e.g., using `mulmod` as in OpenZeppelin's `Math.mulDiv`):
```solidity
function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    uint256 result = mulDiv(x, y, d); // 512-bit safe
    if (mulmod(x, y, d) > 0) result += 1;
    return result;
}
```
Alternatively, add an explicit upper-bound check on `targetBuyerAssets` in `buyerAssetsToUnits` before calling `mulDivUp`, or wrap the `consumableUnits` call in `MidnightBundles` in a `try/catch` so that a single malicious offer is skipped rather than reverting the entire bundle.

## Proof of Concept
```solidity
// Minimal Foundry unit test
function test_consumableUnits_overflow() public {
    Offer memory offer = Offer({
        buy: true,
        maxUnits: 0,
        maxAssets: type(uint256).max, // > type(uint256).max / 1e18
        // ... other fields set to valid values
    });
    // Expect revert due to overflow in mulDivUp
    vm.expectRevert();
    ConsumableUnitsLib.consumableUnits(address(midnight), id, offer);
}
```
Manual steps:
1. Maker signs an offer with `buy = true`, `maxUnits = 0`, `maxAssets = type(uint256).max`, and any valid `tick`/`market`.
2. Taker (or routing system) constructs a `Take[]` array containing this offer and calls `supplyCollateralAndSellWithUnitsTarget` or `supplyCollateralAndSellWithAssetsTarget` on `MidnightBundles`.
3. The call reverts at `ConsumableUnitsLib.consumableUnits` → `TakeAmountsLib.buyerAssetsToUnits` → `UtilsLib.mulDivUp` due to the `type(uint256).max * 1e18` overflow.

### Citations

**File:** src/periphery/ConsumableUnitsLib.sol (L18-19)
```text
        } else if (offer.buy) {
            return TakeAmountsLib.buyerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
```

**File:** src/periphery/TakeAmountsLib.sol (L27-29)
```text
        uint256 buyerPrice = sellerPrice + settlementFee;
        require(buyerPrice <= WAD, TickLib.PriceGreaterThanOne());
        return offer.buy ? targetBuyerAssets.mulDivUp(WAD, buyerPrice) : targetBuyerAssets.mulDivDown(WAD, buyerPrice);
```

**File:** src/libraries/UtilsLib.sol (L34-36)
```text
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
```

**File:** src/periphery/MidnightBundles.sol (L147-151)
```text
            uint256 unitsToTake = min(
                targetUnits - filledUnits,
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
```

**File:** src/periphery/MidnightBundles.sol (L285-291)
```text
            uint256 unitsToTake = min(
                TakeAmountsLib.sellerAssetsToUnits(
                    MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
                ),
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
```
