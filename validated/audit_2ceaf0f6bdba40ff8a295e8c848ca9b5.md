[1](#0-0) 

Audit Report

## Title
`consumableUnits` Missing Tick Accessibility Check Enables Malicious Maker to Grief Bundler Takers via Silent `OutOfOffers` - (File: src/periphery/ConsumableUnitsLib.sol)

## Summary
`ConsumableUnitsLib.consumableUnits` returns a positive unit count for any offer with remaining capacity without verifying that `offer.tick` is divisible by the market's `tickSpacing`. When the bundler uses this value to call `take()`, the core contract's unconditional `TickNotAccessible` revert is silently swallowed by the `catch {}` block. If insufficient valid offers remain to fill the target, the bundler reverts with `OutOfOffers`, causing the taker to lose gas and fail their intended operation.

## Finding Description

**Root cause — missing check in `consumableUnits`:**

`ConsumableUnitsLib.consumableUnits` computes remaining capacity purely from `consumed`, `maxUnits`, and `maxAssets` with no tick divisibility check:

```solidity
// src/periphery/ConsumableUnitsLib.sol:14-23
function consumableUnits(address midnight, bytes32 id, Offer memory offer) internal view returns (uint256) {
    uint256 consumed = IMidnight(midnight).consumed(offer.maker, offer.group);
    if (offer.maxUnits > 0) {
        return offer.maxUnits.zeroFloorSub(consumed);   // no tick check
    } else if (offer.buy) {
        return TakeAmountsLib.buyerAssetsToUnits(...);   // no tick check
    } else {
        return TakeAmountsLib.sellerAssetsToUnits(...);  // no tick check
    }
}
```

**Core contract enforces the check unconditionally inside `take()`:**

`src/Midnight.sol` contains an unconditional `require(offer.tick % _marketState.tickSpacing == 0, TickNotAccessible())` that fires before any units-related logic, so even a call with `units > 0` reverts for an inaccessible tick. `DEFAULT_TICK_SPACING = 4` is set for all markets.

**Bundler swallows the revert across all four entry-points:**

All four bundler functions share the same pattern:

```solidity
// e.g. src/periphery/MidnightBundles.sol:74-85
uint256 unitsToTake = min(
    targetUnits - filledUnits,
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)  // returns non-zero
);
try IMidnight(MIDNIGHT).take(takes[i].offer, ..., unitsToTake, ...) returns (...) {
    filledUnits += unitsToTake;
    ...
} catch {}   // TickNotAccessible silently swallowed
```

The NatSpec at line 42 explicitly documents: *"Skips every reason why take can revert (including ones that are not asynchrony related)."*

After the loop, insufficient fill triggers:
```solidity
require(filledUnits == targetUnits, OutOfOffers());  // lines 88, 163, 224, 303
```

**Exploit flow:**
1. Market has `tickSpacing=4` (default for all markets via `DEFAULT_TICK_SPACING = 4`).
2. Malicious maker signs `Offer{tick: 2921, maxUnits: type(uint256).max, ...}` — valid EIP-712 data, passes ratifier checks.
3. Offer is published to the off-chain order book.
4. Aggregator calls `consumableUnits`; it returns `type(uint256).max` (no tick check).
5. Aggregator includes the offer in `takes[]` and calls a bundler entry-point.
6. `unitsToTake` is non-zero; `take()` reverts with `TickNotAccessible`.
7. `catch {}` swallows the revert; `filledUnits` is not incremented.
8. If no other offers cover `targetUnits`, the bundler reverts with `OutOfOffers`.

**Why existing checks fail:** The bundler's `InconsistentMarket` and `InconsistentSide` guards (lines 72–73, 145–146, 206–207, 283–284) only check market ID and buy/sell side — neither validates tick accessibility.

## Impact Explanation
A malicious maker can reliably cause any taker's bundler transaction to revert with `OutOfOffers` by polluting the order book with inaccessible-tick offers. The taker loses gas and fails to complete their intended borrow or lend operation. Because the bundler is the documented integration path for takers, this constitutes concrete user-facing griefing through normal protocol usage. The impact is service degradation / denial of service for takers, not direct asset theft.

## Likelihood Explanation
All preconditions are trivially satisfiable by any unprivileged actor: (1) all markets start with `DEFAULT_TICK_SPACING = 4`; (2) any maker can sign an offer with `tick % 4 != 0` at zero on-chain cost; (3) any aggregator or frontend that uses `consumableUnits` to assess offers will include the poisoned offer. The attack is repeatable indefinitely, requires no on-chain state, and is undetectable by `consumableUnits` as currently written.

## Recommendation
Add a tick accessibility check inside `ConsumableUnitsLib.consumableUnits` that returns `0` when `offer.tick % marketState.tickSpacing != 0`. This requires reading `tickSpacing` from the market state (via `IMidnight`). Alternatively, the bundler loop can skip offers where `offer.tick % market.tickSpacing != 0` before computing `unitsToTake`. The library fix is preferred as it closes the gap for any future consumer of `consumableUnits`.

```solidity
function consumableUnits(address midnight, bytes32 id, Offer memory offer) internal view returns (uint256) {
    // Add: fetch tickSpacing and return 0 for inaccessible ticks
    uint8 tickSpacing = IMidnight(midnight).marketState(id).tickSpacing;
    if (offer.tick % tickSpacing != 0) return 0;
    
    uint256 consumed = IMidnight(midnight).consumed(offer.maker, offer.group);
    // ... rest unchanged
}
```

## Proof of Concept
**Minimal forge test plan:**
1. Deploy `Midnight` and `MidnightBundles` on a local fork.
2. Create a market (gets `DEFAULT_TICK_SPACING = 4`).
3. Malicious maker signs an offer with `tick = 1` (since `1 % 4 != 0`) and `maxUnits = type(uint256).max`.
4. Legitimate taker calls `buyWithUnitsTargetAndWithdrawCollateral` with `targetUnits = 100` and `takes = [poisonedOffer]` as the only entry.
5. Observe: `consumableUnits` returns `type(uint256).max`; `unitsToTake = 100`; `take()` reverts with `TickNotAccessible`; `catch {}` swallows it; `filledUnits == 0`; final `require(0 == 100, OutOfOffers())` reverts.
6. Confirm the same behavior for all four bundler entry-points. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) [10](#0-9) [11](#0-10)

### Citations

**File:** src/periphery/ConsumableUnitsLib.sol (L14-23)
```text
    function consumableUnits(address midnight, bytes32 id, Offer memory offer) internal view returns (uint256) {
        uint256 consumed = IMidnight(midnight).consumed(offer.maker, offer.group);
        if (offer.maxUnits > 0) {
            return offer.maxUnits.zeroFloorSub(consumed);
        } else if (offer.buy) {
            return TakeAmountsLib.buyerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
        } else {
            return TakeAmountsLib.sellerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
        }
    }
```

**File:** src/libraries/ConstantsLib.sol (L26-26)
```text
uint8 constant DEFAULT_TICK_SPACING = 4;
```

**File:** src/periphery/MidnightBundles.sol (L42-42)
```text
    /// @dev Skips every reason why take can revert (including ones that are not asynchrony related).
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

**File:** src/periphery/MidnightBundles.sol (L88-88)
```text
        require(filledUnits == targetUnits, OutOfOffers());
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

**File:** src/periphery/MidnightBundles.sol (L163-163)
```text
        require(filledUnits == targetUnits, OutOfOffers());
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

**File:** src/periphery/MidnightBundles.sol (L224-224)
```text
        require(filledBuyerAssets == targetFilledBuyerAssets, OutOfOffers());
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

**File:** src/periphery/MidnightBundles.sol (L303-303)
```text
        require(filledSellerAssets == targetFilledSellerAssets, OutOfOffers());
```
