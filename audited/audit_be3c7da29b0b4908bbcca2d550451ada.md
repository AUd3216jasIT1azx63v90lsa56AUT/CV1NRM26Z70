All code references verified against the actual source. Every cited line, function, and behavior matches exactly.

---

Audit Report

## Title
Front-Running Partial Fill Causes Exact-Equality DoS in `buyWithAssetsTargetAndWithdrawCollateral` and `supplyCollateralAndSellWithAssetsTarget` - (File: src/periphery/MidnightBundles.sol)

## Summary

`buyWithAssetsTargetAndWithdrawCollateral` enforces `filledBuyerAssets == targetFilledBuyerAssets` at line 224. The `unitsToTake` per offer is capped by `ConsumableUnitsLib.consumableUnits`, which reads live on-chain `consumed[maker][group]` state at execution time. Any third party who takes even a minimal amount from a target offer before the bundle executes will reduce `consumableUnits`, causing the bundle to underfill and revert with `OutOfOffers`. The identical flaw exists in `supplyCollateralAndSellWithAssetsTarget` at line 303.

## Finding Description

**Consumed tracking — `Midnight.sol` lines 367–369:**
Each `take()` call increments `consumed[offer.maker][offer.group]` by the assets transacted and enforces the cap:

```solidity
newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
require(newConsumed <= offer.maxAssets, ConsumedAssets());
``` [1](#0-0) 

**`ConsumableUnitsLib.consumableUnits` — lines 14–22:**
The remaining capacity is computed by reading live state with no snapshot:

```solidity
uint256 consumed = IMidnight(midnight).consumed(offer.maker, offer.group);
// ...
return TakeAmountsLib.sellerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
``` [2](#0-1) 

Any increase in `consumed` between bundle submission and execution directly reduces the returned unit count.

**Bundle loop and exact equality check — `MidnightBundles.sol` lines 208–224:**

```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.buyerAssetsToUnits(..., targetFilledBuyerAssets - filledBuyerAssets),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)  // live state
);
try IMidnight(MIDNIGHT).take(..., unitsToTake, ...) returns (uint256 resBuyerAssets, uint256) {
    filledBuyerAssets += resBuyerAssets;
} catch {}
// ...
require(filledBuyerAssets == targetFilledBuyerAssets, OutOfOffers());
``` [3](#0-2) 

**Exploit flow:**
1. Victim submits `buyWithAssetsTargetAndWithdrawCollateral` with offer O (`maxAssets` set), `targetFilledBuyerAssets = X`.
2. Attacker front-runs with `Midnight.take(O, smallUnits)`, consuming Y assets from offer O. This is permissionless.
3. `consumed[O.maker][O.group]` increases by Y.
4. Victim's bundle executes: `consumableUnits(O)` = `sellerAssetsToUnits(maxAssets - Y)` < full capacity.
5. `unitsToTake` is capped at the reduced value; `take` succeeds but `resBuyerAssets < targetFilledBuyerAssets`.
6. Loop ends (no more offers); `filledBuyerAssets < targetFilledBuyerAssets`.
7. `require(filledBuyerAssets == targetFilledBuyerAssets, OutOfOffers())` reverts.

**Why existing checks do not stop it:**
- The `try/catch` only handles reverts from `take`; it does not handle the case where `take` succeeds but fills fewer assets than needed.
- There is no slippage tolerance or minimum-fill threshold — the check is strict equality.
- There is no snapshot of `consumed` at bundle submission time.
- The attacker's `take` is fully valid and passes all core checks.

The identical pattern exists in `supplyCollateralAndSellWithAssetsTarget` at line 303, where `require(filledSellerAssets == targetFilledSellerAssets, OutOfOffers())` enforces the same strict equality against a live-state-capped fill. [4](#0-3) 

## Impact Explanation

Any unprivileged actor holding loan tokens can repeatedly grief a victim's `buyWithAssetsTargetAndWithdrawCollateral` (or `supplyCollateralAndSellWithAssetsTarget`) bundle by front-running with a minimal partial fill of the target offer. The victim's transaction reverts with `OutOfOffers` every time, permanently blocking the bundle from executing as long as the attacker repeats the front-run. The victim's tokens are returned on revert, but the victim cannot complete the intended market action via the bundle. This constitutes service unavailability and severe degradation of a core user-facing function under realistic attacker input, matching the impact class in RESEARCHER.md. [5](#0-4) 

## Likelihood Explanation

**Preconditions:**
- Victim uses `buyWithAssetsTargetAndWithdrawCollateral` with an offer that has `maxAssets > 0`.
- The victim's `takes` array has insufficient fallback offers to absorb the capacity reduction (e.g., a single-offer bundle, or the attacker front-runs all listed offers).
- Attacker holds any nonzero amount of loan tokens.

**Feasibility:** High. Taking an offer is permissionless. The attacker needs only a minimal amount of loan tokens (even 1 unit of sellerAssets) to reduce `consumableUnits` by at least 1, which is sufficient to break the exact equality. The attacker receives a market position (credit) in return, so the net cost is gas plus any spread, not the full loan amount. The attack is repeatable every block. [6](#0-5) 

## Recommendation

Replace the strict equality check with a `>=` check and add a minimum-fill guard:

```solidity
require(filledBuyerAssets >= targetFilledBuyerAssets, OutOfOffers());
```

Alternatively, allow the caller to specify a `minFilledBuyerAssets` tolerance parameter so partial fills within an acceptable range succeed. For `supplyCollateralAndSellWithAssetsTarget`, apply the symmetric fix at line 303. Additionally, consider snapshotting `consumed` values off-chain and passing them as hints, or documenting that callers must supply sufficient fallback offers to absorb front-running capacity reduction. [7](#0-6) 

## Proof of Concept

**Minimal fork test plan:**

1. Deploy `Midnight` and `MidnightBundles` on a local fork.
2. Create a sell offer O with `maxAssets = 1000`, `group = G`.
3. Victim calls `buyWithAssetsTargetAndWithdrawCollateral` with `takes = [O]`, `targetBuyerAssets` corresponding to consuming all 1000 assets.
4. Before the victim's tx lands, attacker calls `Midnight.take(O, 1 unit)` — consuming a small amount of assets, incrementing `consumed[O.maker][G]`.
5. Victim's tx executes: `consumableUnits(O)` is now reduced; `unitsToTake` is capped below what's needed; `take` succeeds with `resBuyerAssets < targetFilledBuyerAssets`.
6. `require(filledBuyerAssets == targetFilledBuyerAssets, OutOfOffers())` at line 224 reverts.
7. Confirm victim's tokens are returned (tx rolled back) but the bundle did not execute.
8. Repeat step 4 each block to demonstrate permanent griefing. [8](#0-7)

### Citations

**File:** src/Midnight.sol (L367-369)
```text
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/periphery/ConsumableUnitsLib.sol (L14-22)
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
```

**File:** src/periphery/MidnightBundles.sol (L197-225)
```text
        pullToken(loanToken, msg.sender, targetBuyerAssets, loanTokenPermit);
        forceApproveMax(loanToken, MIDNIGHT);

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

**File:** src/periphery/MidnightBundles.sol (L285-303)
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
        }

        require(filledSellerAssets == targetFilledSellerAssets, OutOfOffers());
```

**File:** RESEARCHER.md (L14-15)
```markdown
- Permanent lock, freeze, or unrecoverable corruption of user/project state.
- Service unavailability or severe degradation under realistic attacker input.
```
