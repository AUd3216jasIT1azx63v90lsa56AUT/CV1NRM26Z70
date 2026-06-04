### Title
Exact-Fill Requirement in MidnightBundles Asset-Target Functions Enables Frontrunning DoS — (File: src/periphery/MidnightBundles.sol)

### Summary
`buyWithAssetsTargetAndWithdrawCollateral` and `supplyCollateralAndSellWithAssetsTarget` in `MidnightBundles.sol` enforce a strict equality check requiring that the filled asset amount equals the target exactly. This is the direct analog of `amountDesired == amountMin` in the external report: any partial consumption of a listed offer by a frontrunner causes the entire transaction to revert, denying service to the user.

### Finding Description

**Root cause — exact fill enforced with `==`:**

In `buyWithAssetsTargetAndWithdrawCollateral`: [1](#0-0) 

```solidity
require(filledBuyerAssets == targetFilledBuyerAssets, OutOfOffers());
require(filledUnits >= minUnits, UnitsTooLow());
```

In `supplyCollateralAndSellWithAssetsTarget`: [2](#0-1) 

```solidity
require(filledSellerAssets == targetFilledSellerAssets, OutOfOffers());
require(filledUnits <= maxUnits, UnitsTooHigh());
```

**Exploit path:**

The inner loop uses `TakeAmountsLib` to compute `unitsToTake` that would yield exactly the remaining target assets, then takes the minimum with `ConsumableUnitsLib.consumableUnits`: [3](#0-2) 

```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.buyerAssetsToUnits(
        MIDNIGHT, id, takes[i].offer, targetFilledBuyerAssets - filledBuyerAssets
    ),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
);
```

If an attacker frontruns and partially consumes any offer in the `takes` array (advancing `consumed[maker][group]`), `ConsumableUnitsLib.consumableUnits` returns fewer units than needed. The `min(...)` clamps `unitsToTake` downward, so `resBuyerAssets < (targetFilledBuyerAssets - filledBuyerAssets)`. After exhausting all offers, `filledBuyerAssets < targetFilledBuyerAssets`, and the strict equality check reverts with `OutOfOffers`.

The same pattern applies to `supplyCollateralAndSellWithAssetsTarget` on the seller side. [4](#0-3) 

**Contrast with the units-target variants**, which have the same structural `==` check on units but expose `maxBuyerAssets` / `minSellerAssets` as explicit slippage guards on the asset side: [5](#0-4) [6](#0-5) 

The asset-target functions have no equivalent tolerance parameter — the equality is the only check.

### Impact Explanation

Every call to `buyWithAssetsTargetAndWithdrawCollateral` or `supplyCollateralAndSellWithAssetsTarget` can be griefed by a frontrunner who partially fills any one of the listed offers before the victim's transaction lands. The victim's transaction reverts, wasting gas and delaying the intended operation (e.g., buying units to repay debt and then withdrawing collateral). A sustained attacker can keep the user's position in limbo indefinitely at the cost of repeated frontrun transactions. Funds are not permanently lost (the transaction reverts atomically), but the user's ability to interact with the protocol through the bundler is effectively blocked.

### Likelihood Explanation

Any unprivileged external actor can take offers on Midnight directly via `IMidnight.take`. No special access is required. On chains with a public mempool (Ethereum mainnet, most L2s), a bot watching for bundler calls can trivially frontrun by submitting a direct `take` on the same offer with higher gas. The attacker only needs to partially consume one offer in the victim's list to trigger the revert. This is a realistic, low-cost griefing vector.

### Recommendation

Replace the strict equality with a range check. For `buyWithAssetsTargetAndWithdrawCollateral`, accept a `minFilledBuyerAssets` parameter and check:

```solidity
require(filledBuyerAssets >= minFilledBuyerAssets, FilledAssetsTooLow());
```

Return the unspent portion of `targetBuyerAssets` to `msg.sender` regardless. For `supplyCollateralAndSellWithAssetsTarget`, accept a `minFilledSellerAssets` parameter analogously. This mirrors the design already present in `supplyCollateralAndSellWithUnitsTarget` (line 166), which correctly uses `>=` rather than `==`. [7](#0-6) 

### Proof of Concept

1. User submits `buyWithAssetsTargetAndWithdrawCollateral` with `targetBuyerAssets = 1000e18`, a single offer `O` with capacity for 1000e18 assets, and `minUnits = X`.
2. Attacker observes the pending transaction in the mempool.
3. Attacker calls `IMidnight(MIDNIGHT).take(O, ..., 1, ...)` directly with higher gas, consuming 1 unit from offer `O`.
4. `ConsumableUnitsLib.consumableUnits` now returns units corresponding to `<1000e18` assets for offer `O`.
5. `unitsToTake` is clamped to the reduced capacity; `resBuyerAssets < 1000e18`.
6. Loop ends; `filledBuyerAssets < targetFilledBuyerAssets`.
7. `require(filledBuyerAssets == targetFilledBuyerAssets, OutOfOffers())` reverts.
8. User's transaction fails; attacker repeats on every retry. [8](#0-7)

### Citations

**File:** src/periphery/MidnightBundles.sol (L88-88)
```text
        require(filledUnits == targetUnits, OutOfOffers());
```

**File:** src/periphery/MidnightBundles.sol (L163-166)
```text
        require(filledUnits == targetUnits, OutOfOffers());

        uint256 referralFeeAssets = filledSellerAssets.mulDivDown(referralFeePct, WAD);
        require(filledSellerAssets - referralFeeAssets >= minSellerAssets, SellerAssetsTooLow());
```

**File:** src/periphery/MidnightBundles.sol (L200-225)
```text
        uint256 referralFeeAssets = targetBuyerAssets.mulDivDown(referralFeePct, WAD);
        uint256 targetFilledBuyerAssets = targetBuyerAssets - referralFeeAssets;

        uint256 filledUnits;
        uint256 filledBuyerAssets;
        for (uint256 i; i < takes.length && filledBuyerAssets < targetFilledBuyerAssets; i++) {
            require(!takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
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
        }

        require(filledBuyerAssets == targetFilledBuyerAssets, OutOfOffers());
        require(filledUnits >= minUnits, UnitsTooLow());
```

**File:** src/periphery/MidnightBundles.sol (L282-300)
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

**File:** src/periphery/MidnightBundles.sol (L303-304)
```text
        require(filledSellerAssets == targetFilledSellerAssets, OutOfOffers());
        require(filledUnits <= maxUnits, UnitsTooHigh());
```
