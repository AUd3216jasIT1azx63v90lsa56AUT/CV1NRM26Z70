Audit Report

## Title
`consumableUnits` called outside `try/catch` reverts entire bundle on out-of-range tick - (File: `src/periphery/MidnightBundles.sol`)

## Summary
All four bundle entry points in `MidnightBundles` invoke `ConsumableUnitsLib.consumableUnits()` as an argument to `min(...)` before the `try` keyword, meaning any revert inside `consumableUnits` propagates out of the bundle function entirely. When an offer has `maxUnits == 0` and `maxAssets > 0`, `consumableUnits` delegates to `TakeAmountsLib`, which calls `TickLib.tickToPrice(offer.tick)` unconditionally. If `offer.tick > MAX_TICK (5820)`, `tickToPrice` reverts with `TickOutOfRange`, bypassing the `try/catch` and reverting the entire bundle transaction, DoS-ing all legitimate takes in the bundle.

## Finding Description

**Root cause — `consumableUnits` evaluated before `try`:**

In all four bundle functions, the `min(...)` call that computes `unitsToTake` is evaluated as a Solidity expression before the `try` keyword. Any revert inside that expression propagates to the caller, not into the `catch` block.

`buyWithUnitsTargetAndWithdrawCollateral` (lines 74–85): [1](#0-0) 

`supplyCollateralAndSellWithUnitsTarget` (lines 147–160): [2](#0-1) 

`buyWithAssetsTargetAndWithdrawCollateral` (lines 208–221): [3](#0-2) 

`supplyCollateralAndSellWithAssetsTarget` (lines 285–300): [4](#0-3) 

The NatSpec at line 43 explicitly acknowledges this: `"Reverts if ConsumableUnitsLib reverts"`. [5](#0-4) 

**`ConsumableUnitsLib.consumableUnits` branches on `maxUnits`:**

When `offer.maxUnits == 0`, it calls `TakeAmountsLib.buyerAssetsToUnits` or `TakeAmountsLib.sellerAssetsToUnits`: [6](#0-5) 

**Both `TakeAmountsLib` functions call `tickToPrice` unconditionally as their first statement:** [7](#0-6) [8](#0-7) 

**`tickToPrice` reverts for any tick exceeding `MAX_TICK = 5820`:** [9](#0-8) [10](#0-9) 

**Existing guards are insufficient:**

The only tick validation in the protocol — `require(offer.tick % _marketState.tickSpacing == 0, TickNotAccessible())` — lives inside `IMidnight.take()`, which is inside the `try` block. It does not execute before `consumableUnits` is called. There is no on-chain validation of `offer.tick` prior to the `min(...)` expression.

**Exploit flow:**
1. Attacker (maker) signs an offer off-chain with `offer.tick = 5821` (any value > 5820) and `offer.maxAssets > 0` (ensuring `offer.maxUnits == 0`).
2. The poisoned offer is included anywhere in a taker's `takes[]` array alongside legitimate offers.
3. When the loop reaches the poisoned offer, `consumableUnits` → `buyerAssetsToUnits`/`sellerAssetsToUnits` → `tickToPrice(5821)` reverts with `TickOutOfRange`.
4. The revert propagates outside the `try/catch`, reverting the entire bundle transaction and failing all legitimate takes.

## Impact Explanation
Complete DoS of any bundle transaction containing the poisoned offer. All legitimate takes in the bundle fail atomically. In time-sensitive contexts (near maturity, liquidation windows, volatile markets), the taker suffers real financial loss from failed execution. The attack is repeatable at zero cost to the attacker.

## Likelihood Explanation
Any maker can sign an offer with an out-of-range tick at zero cost (off-chain signing, no gas, no on-chain state, no privileged access). The only precondition is that the poisoned offer appears in the `takes` array with `maxAssets > 0`. Automated routing or aggregation systems that fetch offers from an off-chain order book are the primary target and cannot distinguish a poisoned offer from a legitimate one without pre-validating `offer.tick` client-side. The attack is trivially repeatable.

## Recommendation
Move the `consumableUnits` (and `TakeAmountsLib`) calls inside the `try/catch` block, or wrap them in a separate `try/catch` that treats any revert as `unitsToTake = 0` (causing the offer to be skipped). Alternatively, add an explicit `if (offer.tick > MAX_TICK) continue;` guard at the top of each loop iteration before any call that invokes `tickToPrice`.

## Proof of Concept
**Minimal manual steps:**
1. Deploy `MidnightBundles` pointing at a live/fork `Midnight` instance.
2. Construct a `Take` with `offer.tick = 5821`, `offer.maxUnits = 0`, `offer.maxAssets = 1e18` (any nonzero value), and a valid maker signature.
3. Place this poisoned `Take` at any index in a `takes[]` array that also contains valid, fillable offers.
4. Call `buyWithUnitsTargetAndWithdrawCollateral` (or any of the other three bundle functions) with this array.
5. Observe the transaction reverts with `TickOutOfRange` rather than skipping the poisoned offer and filling the legitimate ones.

**Fuzz test plan:** Fuzz `offer.tick` over the range `[MAX_TICK+1, type(uint256).max]` with `offer.maxUnits = 0` and `offer.maxAssets > 0`; assert that all four bundle entry points revert rather than returning normally.

### Citations

**File:** src/periphery/MidnightBundles.sol (L43-43)
```text
    /// @dev Reverts if ConsumableUnitsLib reverts.
```

**File:** src/periphery/MidnightBundles.sol (L74-85)
```text
            uint256 unitsToTake = min(
                targetUnits - filledUnits,
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

**File:** src/periphery/MidnightBundles.sol (L147-160)
```text
            uint256 unitsToTake = min(
                targetUnits - filledUnits,
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

**File:** src/periphery/ConsumableUnitsLib.sol (L16-22)
```text
        if (offer.maxUnits > 0) {
            return offer.maxUnits.zeroFloorSub(consumed);
        } else if (offer.buy) {
            return TakeAmountsLib.buyerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
        } else {
            return TakeAmountsLib.sellerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
        }
```

**File:** src/periphery/TakeAmountsLib.sol (L22-22)
```text
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
```

**File:** src/periphery/TakeAmountsLib.sol (L41-41)
```text
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
```

**File:** src/libraries/TickLib.sol (L6-6)
```text
uint256 constant MAX_TICK = 5820;
```

**File:** src/libraries/TickLib.sol (L44-45)
```text
    function tickToPrice(uint256 tick) internal pure returns (uint256) {
        require(tick <= MAX_TICK, TickOutOfRange());
```
