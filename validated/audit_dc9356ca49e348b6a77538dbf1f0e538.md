Audit Report

## Title
TOCTOU Stale `consumableUnits` Read Causes Bundler to Skip Partially-Filled Offers and Revert with `OutOfOffers` - (File: src/periphery/ConsumableUnitsLib.sol)

## Summary
`ConsumableUnitsLib.consumableUnits` reads `consumed[maker][group]` from live on-chain state and returns remaining offer capacity. In all four `MidnightBundles` entry-points, this value is used to compute `unitsToTake` before calling `take()`. If any taker fills even 1 unit of the same offer between the `consumableUnits` read and the `take()` call, `unitsToTake` will exceed the actual remaining capacity, causing `take()` to revert with `ConsumedUnits` or `ConsumedAssets`. The broad `catch {}` block silently drops the offer entirely — even though it retains remaining capacity — and if no subsequent offers cover the shortfall, the bundle reverts with `OutOfOffers`.

## Finding Description

**Root cause — stale snapshot in `ConsumableUnitsLib.consumableUnits`:**

`src/periphery/ConsumableUnitsLib.sol` lines 14–17 read the live `consumed` mapping and compute remaining capacity:

```solidity
uint256 consumed = IMidnight(midnight).consumed(offer.maker, offer.group);
if (offer.maxUnits > 0) {
    return offer.maxUnits.zeroFloorSub(consumed);   // snapshot, not atomic
```

`src/periphery/MidnightBundles.sol` lines 74–78 use this snapshot to compute `unitsToTake`:

```solidity
uint256 unitsToTake = min(
    targetUnits - filledUnits,
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
);
```

`src/Midnight.sol` lines 371–372 then increment `consumed` **before** enforcing the cap:

```solidity
newConsumed = consumed[offer.maker][offer.group] += units;
require(newConsumed <= offer.maxUnits, ConsumedUnits());
```

If `consumed` increased between the `consumableUnits` read and this line, `newConsumed` overshoots `maxUnits` and reverts. The bundler's `catch {}` at `MidnightBundles.sol` lines 85 / 160 / 221 / 300 swallows the revert and skips the offer with no retry:

```solidity
} catch {}
```

`filledUnits` is not incremented, and the loop moves on. If no remaining offers cover the shortfall, `MidnightBundles.sol` line 88 reverts:

```solidity
require(filledUnits == targetUnits, OutOfOffers());
```

**Exploit flow (`maxUnits = 100`, `consumed = 0`, `targetUnits = 100`, single offer):**

1. Victim submits bundle via `buyWithUnitsTargetAndWithdrawCollateral`.
2. `consumableUnits` reads `consumed = 0`, returns `100`. `unitsToTake = 100`.
3. Attacker (any unprivileged taker on an ungated offer) front-runs with `take(offer, ..., 1, ...)` → `consumed` becomes `1`.
4. Victim's `take(offer, ..., 100, ...)` executes: `newConsumed = 1 + 100 = 101 > 100` → reverts `ConsumedUnits`.
5. `catch {}` swallows the revert; `filledUnits` stays `0`.
6. Loop ends. `require(0 == 100, OutOfOffers())` → bundle reverts.

The offer still had 99 units of remaining capacity. The same TOCTOU applies to the `maxAssets` branch (`ConsumedAssets`) and to all four bundle entry-points (`buyWithUnitsTargetAndWithdrawCollateral`, `supplyCollateralAndSellWithUnitsTarget`, `buyWithAssetsTargetAndWithdrawCollateral`, `supplyCollateralAndSellWithAssetsTarget`).

**Why existing checks do not stop it:**

- The `catch {}` is intentionally broad (NatSpec: *"Skips every reason why take can revert (including ones that are not asynchrony related)"*), so `ConsumedUnits`/`ConsumedAssets` are caught and the offer is dropped.
- There is no retry with a refreshed `consumableUnits` value after a failed `take()`.
- The attacker only needs to fill **1 unit** to shift `consumed` enough that `unitsToTake` (computed from stale state) overshoots the remaining capacity.

## Impact Explanation
Any unprivileged taker can grief a victim's bundle by front-running with a minimal fill of any offer in the bundle's `takes` array. The victim's transaction reverts with `OutOfOffers`, their intended borrow or lend does not execute, and gas is wasted. The attack is repeatable: the attacker can re-front-run every retry the victim makes, permanently blocking the operation as long as the attacker is willing to pay gas. This constitutes a concrete, repeatable denial-of-service against the bundler's core functionality.

## Likelihood Explanation
Preconditions are minimal: the attacker must observe the pending bundle transaction (standard mempool visibility) and must be authorized to call `take()` on the same offer (any address qualifies if the offer has no `enterGate`). The attacker's cost is one `take()` call for 1 unit. The victim's bundle fails entirely. The attack is repeatable at low cost and requires no privileged access.

## Recommendation
After a failed `take()`, re-read `consumableUnits` and retry with the updated (smaller) value before skipping the offer. Alternatively, decode the revert reason inside the `catch` block: if the error is `ConsumedUnits` or `ConsumedAssets`, re-query the remaining capacity and retry with `min(unitsToTake, freshConsumableUnits)` rather than dropping the offer entirely. A simpler mitigation is to call `IMidnight(MIDNIGHT).consumed(offer.maker, offer.group)` immediately before constructing `unitsToTake` inside the `try` block, or to use a single atomic helper that reads and takes in one call.

## Proof of Concept
**Minimal Foundry fork test outline:**

1. Deploy `Midnight` and `MidnightBundles`.
2. Create a market; maker publishes an offer with `maxUnits = 100`.
3. Victim calls `buyWithUnitsTargetAndWithdrawCollateral` with `targetUnits = 100` and a single-element `takes` array pointing to the offer.
4. Before the victim's transaction is mined, attacker calls `Midnight.take(offer, ..., 1, ...)` directly (1 unit).
5. Mine the victim's transaction.
6. Assert the transaction reverts with `OutOfOffers`.
7. Assert `consumed[maker][group] == 1` (offer still has 99 units remaining).