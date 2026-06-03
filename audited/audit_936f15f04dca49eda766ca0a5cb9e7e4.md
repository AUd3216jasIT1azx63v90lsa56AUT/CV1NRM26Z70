Audit Report

## Title
`mulDivUp` Arithmetic Overflow in `consumableUnits` for Buy Offers with Oversized `maxAssets` - (File: `src/periphery/ConsumableUnitsLib.sol`)

## Summary
When `consumableUnits` is called for a buy offer with `maxUnits == 0` and `maxAssets > type(uint256).max / WAD` (~1.157e59), the call to `TakeAmountsLib.buyerAssetsToUnits` triggers an unchecked `targetBuyerAssets * WAD` multiplication in `UtilsLib.mulDivUp`, which overflows and reverts under Solidity 0.8.x checked arithmetic. Because `MidnightBundles.supplyCollateralAndSellWithUnitsTarget` and `supplyCollateralAndSellWithAssetsTarget` invoke `consumableUnits` outside any `try/catch`, any bundle containing such a crafted offer reverts entirely.

## Finding Description

**Step 1 — `ConsumableUnitsLib.consumableUnits` (line 18–19):**
With `offer.maxUnits == 0` and `offer.buy == true`, the function enters the `else if` branch:
```solidity
return TakeAmountsLib.buyerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
```
`zeroFloorSub` floors at zero but imposes no upper cap. With `consumed == 0`, `targetBuyerAssets = offer.maxAssets` with no magnitude check.

**Step 2 — `TakeAmountsLib.buyerAssetsToUnits` (line 29):**
```solidity
return offer.buy ? targetBuyerAssets.mulDivUp(WAD, buyerPrice) : ...
```
`targetBuyerAssets` is passed as `x` to `mulDivUp` with `y = WAD = 1e18`. The only guard at line 28 (`require(buyerPrice <= WAD)`) bounds `buyerPrice`, not `targetBuyerAssets`.

**Step 3 — `UtilsLib.mulDivUp` (lines 34–36):**
```solidity
function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y + (d - 1)) / d;
}
```
No `unchecked` block. Under Solidity `^0.8.0`, `x * y` reverts on overflow when `x > type(uint256).max / y`. With `y = 1e18`, any `targetBuyerAssets > ~1.157e59` triggers this revert deterministically.

**Propagation to `MidnightBundles`:**
`supplyCollateralAndSellWithUnitsTarget` (line 150) and `supplyCollateralAndSellWithAssetsTarget` (line 290) both call `ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)` directly inside the loop, outside any `try/catch`. The `try/catch` wraps only the subsequent `IMidnight(MIDNIGHT).take(...)` call. The NatSpec at lines 111 and 175 explicitly documents: *"Reverts if ConsumableUnitsLib reverts."*

## Impact Explanation
Any call to `consumableUnits` for a buy offer where `offer.maxAssets > type(uint256).max / 1e18` reverts unconditionally. This causes `supplyCollateralAndSellWithUnitsTarget` and `supplyCollateralAndSellWithAssetsTarget` in `MidnightBundles` to revert for any bundle that includes such an offer. Off-chain routing systems and aggregators that call `consumableUnits` to compute fill amounts are similarly broken for the affected offer class. The core `Midnight.take` is unaffected. Severity is medium: the DoS is scoped to the periphery bundler and to bundles that include the malicious offer, not to the entire contract or core protocol.

## Likelihood Explanation
A maker needs only to sign an offer with `buy = true`, `maxUnits = 0`, and `maxAssets` set to any value above ~1.157e59 (e.g., `type(uint256).max`). No special privilege, no oracle manipulation, and no existing state is required. The condition is trivially reachable on first call with zero prior consumption. Any routing system or aggregator that automatically ingests on-chain or off-chain offers without validating `maxAssets` magnitude can be fed this poisoned offer, causing bundle-level reverts for legitimate takers.

## Recommendation
Add an overflow guard in `UtilsLib.mulDivUp` using an `unchecked` block combined with a pre-multiplication overflow check, or use a full 512-bit `mulDiv` implementation (e.g., from Solady or OpenZeppelin). Alternatively, add an upper-bound check in `ConsumableUnitsLib.consumableUnits` or `TakeAmountsLib.buyerAssetsToUnits` that caps `targetBuyerAssets` at `type(uint256).max / WAD` before passing it to `mulDivUp`. The simplest targeted fix is:
```solidity
function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    unchecked {
        if (y != 0 && x > (type(uint256).max - (d - 1)) / y) revert MulDivOverflow();
        return (x * y + (d - 1)) / d;
    }
}
```
Or replace with a full 512-bit overflow-safe `mulDiv`.

## Proof of Concept
1. Maker signs an offer: `buy = true`, `maxUnits = 0`, `maxAssets = type(uint256).max`, valid `tick`, valid `market`.
2. Taker (or aggregator) constructs a `takes` array containing this offer and calls `supplyCollateralAndSellWithUnitsTarget` or `supplyCollateralAndSellWithAssetsTarget` on `MidnightBundles`.
3. Inside the loop at line 150 (or 290), `ConsumableUnitsLib.consumableUnits` is called.
4. `consumableUnits` calls `buyerAssetsToUnits(midnight, id, offer, type(uint256).max)`.
5. `buyerAssetsToUnits` calls `mulDivUp(type(uint256).max, 1e18, buyerPrice)`.
6. `type(uint256).max * 1e18` overflows → Solidity 0.8.x panics with `Panic(0x11)`.
7. The revert propagates out of `consumableUnits`, out of the loop, and the entire bundle call reverts.

Fuzz test plan: set `offer.buy = true`, `offer.maxUnits = 0`, fuzz `offer.maxAssets` in range `[type(uint256).max / 1e18 + 1, type(uint256).max]`, assert that `consumableUnits` always reverts for this input class.