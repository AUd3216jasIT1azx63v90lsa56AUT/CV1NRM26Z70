### Title
Front-running `buyWithAssetsTargetAndWithdrawCollateral` via partial offer fill causes DoS through exact equality check - (File: src/periphery/MidnightBundles.sol)

### Summary
An unprivileged attacker can front-run a victim's `buyWithAssetsTargetAndWithdrawCollateral` bundle by partially filling the target offer, increasing `consumed[maker][group]` on-chain, causing `ConsumableUnitsLib.consumableUnits` to return fewer units than needed. The bundle's exact equality check `require(filledBuyerAssets == targetFilledBuyerAssets, OutOfOffers())` then fails and the entire bundle reverts. The attacker's cost is only gas; they receive a legitimate fill in return.

### Finding Description

**Root cause — live state read + exact equality check with no slippage tolerance.**

`ConsumableUnitsLib.consumableUnits` reads the live `consumed[maker][group]` value from Midnight at call time: [1](#0-0) 

For a sell offer (`buy == false`) with `maxAssets > 0`, it returns:

```
sellerAssetsToUnits(midnight, id, offer, offer.maxAssets - consumed)
```

In `Midnight.take()`, every successful take of a sell offer with `maxAssets > 0` increments `consumed[maker][group]` by the sellerAssets of that take: [2](#0-1) 

The `consumed` mapping is global and shared across all takers of the same offer group: [3](#0-2) 

Inside `buyWithAssetsTargetAndWithdrawCollateral`, the loop computes `unitsToTake` as the minimum of three values, one of which is `consumableUnits`: [4](#0-3) 

After the loop, the bundle enforces an exact equality: [5](#0-4) 

**Exploit flow:**

1. Maker posts a sell offer with `maxAssets = M`, `maxUnits = 0`. Victim constructs a bundle with `targetBuyerAssets = X`, `takes = [offer]`, requiring `N = buyerAssetsToUnits(X)` units of capacity.
2. Victim broadcasts the bundle transaction.
3. Attacker front-runs with a direct `Midnight.take(offer, ..., Y_units, ...)` where `Y_units >= 1`. This increments `consumed[maker][group]` by `sellerAssets(Y_units)`.
4. Victim's bundle executes. `consumableUnits` now returns `sellerAssetsToUnits(M - consumed_after_attack)`, which is less than `N`.
5. `unitsToTake = min(N, takes[0].units, N - delta) = N - delta` for some `delta > 0`.
6. The take succeeds but fills fewer buyer assets: `filledBuyerAssets = X - epsilon < targetFilledBuyerAssets`.
7. `require(filledBuyerAssets == targetFilledBuyerAssets, OutOfOffers())` reverts.

The `try/catch` around the inner `take` call does not help here because the take itself does not revert — it succeeds with a reduced fill. The revert comes from the post-loop equality check. [6](#0-5) 

The same structural vulnerability exists in `supplyCollateralAndSellWithAssetsTarget` at its own exact equality check: [7](#0-6) 

**Why existing checks do not stop it:**

- The `try/catch` only catches reverts from `take`; a successful take with reduced fill is not caught.
- There is no slippage tolerance or minimum-fill parameter on the assets-target bundle variants.
- `consumableUnits` is a pure view of live state with no snapshot or reservation mechanism.
- The attacker is a legitimate taker; no authorization check prevents them from taking the offer.

### Impact Explanation

Any victim using `buyWithAssetsTargetAndWithdrawCollateral` (or `supplyCollateralAndSellWithAssetsTarget`) targeting a specific offer with `maxAssets` set can be griefed indefinitely. The attacker front-runs each retry with a minimal partial fill (even 1 unit), causing every bundle attempt to revert with `OutOfOffers`. The victim cannot complete their intended trade through the bundler as long as the attacker continues to front-run. The attacker receives a legitimate fill on each front-run, making the attack economically neutral or profitable for them.

### Likelihood Explanation

**Preconditions:**
- Victim uses `buyWithAssetsTargetAndWithdrawCollateral` or `supplyCollateralAndSellWithAssetsTarget` targeting an offer with `maxAssets > 0`.
- Attacker is any address that can call `Midnight.take` on the same offer (no special role required).
- Attacker can observe the mempool (standard on all non-private-mempool chains).

**Feasibility:** High. The attacker needs only to submit a `take` with 1 unit before the victim's bundle lands. The attack is repeatable on every retry. The attacker's only cost is gas; they receive a fill in return.

### Recommendation

Replace the exact equality check with a range check and add a `minBuyerAssets` / `minSellerAssets` slippage parameter, similar to how `buyWithUnitsTargetAndWithdrawCollateral` uses `maxBuyerAssets` with a refund:

```solidity
// Instead of:
require(filledBuyerAssets == targetFilledBuyerAssets, OutOfOffers());

// Use:
require(filledBuyerAssets >= minBuyerAssets, BuyerAssetsTooLow());
// and refund unused tokens:
SafeTransferLib.safeTransfer(loanToken, msg.sender, targetBuyerAssets - filledBuyerAssets - referralFeeAssets);
```

This mirrors the pattern already used in `buyWithUnitsTargetAndWithdrawCollateral` (lines 88, 104) and makes the bundle resilient to front-running by accepting any fill at or above the victim's minimum. [8](#0-7) 

### Proof of Concept

**Foundry stateful fuzz / unit test plan:**

```solidity
// Setup:
// 1. Deploy Midnight + MidnightBundles.
// 2. Maker creates a sell offer: buy=false, maxAssets=1000e18, maxUnits=0, tick=T.
// 3. Victim prepares bundle: targetBuyerAssets=500e18, takes=[offer], minUnits=0.
// 4. Attacker calls Midnight.take(offer, ..., 1 unit, ...) directly.
//    Assert: consumed[maker][group] > 0 after attacker's take.
// 5. Victim calls MidnightBundles.buyWithAssetsTargetAndWithdrawCollateral(...).
//    Assert: call reverts with OutOfOffers().
// 6. Assert: victim's loan tokens are returned (no loss, just DoS).
// 7. Fuzz variant: fuzz attacker's Y_units in [1, maxUnits_needed-1].
//    Assert: bundle always reverts for any Y_units >= 1 that reduces capacity below needed.
```

**Expected assertions:**
- `vm.expectRevert(IMidnightBundles.OutOfOffers.selector)` on the victim's bundle call after attacker's partial fill.
- `assertEq(filledBuyerAssets, 0)` (or `< targetFilledBuyerAssets`) captured via a wrapper.
- Victim's token balance unchanged after the revert (tokens returned by EVM revert).

### Citations

**File:** src/periphery/ConsumableUnitsLib.sol (L15-22)
```text
        uint256 consumed = IMidnight(midnight).consumed(offer.maker, offer.group);
        if (offer.maxUnits > 0) {
            return offer.maxUnits.zeroFloorSub(consumed);
        } else if (offer.buy) {
            return TakeAmountsLib.buyerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
        } else {
            return TakeAmountsLib.sellerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
        }
```

**File:** src/Midnight.sol (L191-191)
```text
    mapping(address user => mapping(bytes32 group => uint256)) public consumed;
```

**File:** src/Midnight.sol (L367-369)
```text
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/periphery/MidnightBundles.sol (L88-104)
```text
        require(filledUnits == targetUnits, OutOfOffers());

        Market memory market = takes[0].offer.market;
        for (uint256 i; i < collateralWithdrawals.length; i++) {
            IMidnight(MIDNIGHT)
                .withdrawCollateral(
                    market,
                    collateralWithdrawals[i].collateralIndex,
                    collateralWithdrawals[i].assets,
                    taker,
                    collateralReceiver
                );
        }

        uint256 referralFeeAssets = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
        if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
        SafeTransferLib.safeTransfer(loanToken, msg.sender, maxBuyerAssets - filledBuyerAssets - referralFeeAssets);
```

**File:** src/periphery/MidnightBundles.sol (L208-214)
```text
            uint256 unitsToTake = min(
                TakeAmountsLib.buyerAssetsToUnits(
                    MIDNIGHT, id, takes[i].offer, targetFilledBuyerAssets - filledBuyerAssets
                ),
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
```

**File:** src/periphery/MidnightBundles.sol (L215-222)
```text
            try IMidnight(MIDNIGHT)
                .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (
                uint256 resBuyerAssets, uint256
            ) {
                filledUnits += unitsToTake;
                filledBuyerAssets += resBuyerAssets;
            } catch {}
        }
```

**File:** src/periphery/MidnightBundles.sol (L224-224)
```text
        require(filledBuyerAssets == targetFilledBuyerAssets, OutOfOffers());
```

**File:** src/periphery/MidnightBundles.sol (L303-303)
```text
        require(filledSellerAssets == targetFilledSellerAssets, OutOfOffers());
```
