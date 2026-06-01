Audit Report

## Title
Gas griefing via malicious ratifier drains transaction gas in `supplyCollateralAndSellWithUnitsTarget` `takes[]` loop - (File: src/periphery/MidnightBundles.sol)

## Summary
An unprivileged attacker can deploy a gas-bomb ratifier contract that burns ~63/64 of forwarded gas before reverting, then create `offer.buy=true` offers pointing to it. When a victim's `supplyCollateralAndSellWithUnitsTarget` call iterates a `takes[]` array containing these offers, the `try/catch` at line 152 catches each revert but cannot recover the gas consumed by the malicious ratifier. After 2–3 iterations, the transaction runs out of gas and reverts entirely, rolling back the victim's collateral supply and wasting their full gas cost.

## Finding Description

**Exact code path:**

`MidnightBundles.supplyCollateralAndSellWithUnitsTarget` iterates `takes[]` with a `try/catch`:

```solidity
for (uint256 i; i < takes.length && filledUnits < targetUnits; i++) {
    require(takes[i].offer.buy, InconsistentSide());          // line 145
    ...
    uint256 unitsToTake = min(
        targetUnits - filledUnits,
        takes[i].units,
        ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)  // line 150
    );
    try IMidnight(MIDNIGHT).take(                              // line 152
        takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(this), address(0), ""
    ) returns (uint256, uint256 resSellerAssets) {
        filledUnits += unitsToTake;
        filledSellerAssets += resSellerAssets;
    } catch {}                                                 // line 160
}
``` [1](#0-0) 

Inside `Midnight.take`, the ratifier is called **before** any consumption check:

```solidity
require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());  // line 355
require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail()); // line 356
``` [2](#0-1) 

The consumption write only occurs at lines 367–373, after the ratifier call:

```solidity
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
} else {
    newConsumed = consumed[offer.maker][offer.group] += units;
    require(newConsumed <= offer.maxUnits, ConsumedUnits());
}
``` [3](#0-2) 

**Attacker-controlled inputs:**

1. Attacker deploys `MaliciousRatifier` whose `isRatified(...)` runs a tight loop consuming ~63/64 of forwarded gas, then reverts.
2. Attacker calls `midnight.setIsAuthorized(maliciousRatifier, true, attacker)` — permissionless. [4](#0-3) 
3. Attacker creates N offers: `offer.buy = true`, `offer.maker = attacker`, `offer.ratifier = maliciousRatifier`, valid tick/expiry/market, `offer.maxUnits` set to a large value.
4. These offers pass `require(takes[i].offer.buy, InconsistentSide())` at line 145. [5](#0-4) 

**Why `consumableUnits` does not skip the call:**

`ConsumableUnitsLib.consumableUnits` returns `offer.maxUnits - consumed[attacker][group]`. Since `take` always reverts before the consumption write (line 371), `consumed` stays 0 and `consumableUnits` returns the full `offer.maxUnits` — a non-zero value — on every iteration. [6](#0-5) 

**EVM gas mechanics (EIP-150):**

Each `try IMidnight(MIDNIGHT).take(...)` forwards at most 63/64 of the caller's remaining gas. When `isRatified` burns all forwarded gas, `take` reverts and returns ~0 gas to `MidnightBundles`. After `catch {}`, `MidnightBundles` retains only ~1/64 of what it had before that iteration. After 2–3 gas-bomb iterations the remaining gas is negligible; the next loop overhead or subsequent valid `take` call triggers OOG, reverting the entire transaction.

**Why existing checks fail:**

- `InconsistentSide`: requires `offer.buy == true` — attacker satisfies this trivially.
- `RatifierUnauthorized`: attacker self-authorizes their own ratifier via the permissionless `setIsAuthorized`.
- `try/catch`: catches the revert from `take` but **cannot refund gas already consumed** by the malicious ratifier. Solidity `try/catch` is not a gas shield.
- `consumableUnits` returning 0: does not apply — the attacker's offers are never consumed because `take` reverts before the consumption write, so `consumableUnits` always returns non-zero.

## Impact Explanation
The victim's `supplyCollateralAndSellWithUnitsTarget` transaction reverts with OOG. Because OOG reverts the entire transaction, the collateral supply (lines 134–140) is also rolled back — no permanent state corruption occurs. The concrete impact is a low-cost, repeatable DoS of the sell-bundle flow: the victim cannot borrow against their collateral via the bundle even when valid offers exist later in `takes[]`, and wastes the full gas cost of the failed transaction on every attempt. [7](#0-6) 

## Likelihood Explanation
All attacker preconditions are permissionless: deploying one gas-bomb ratifier (one-time cost), calling `setIsAuthorized` once, and creating ≥2 offers. The attacker does not need to front-run; they only need their offers to appear in the victim's routing-supplied `takes[]` array. Since routing is off-chain, an attacker can flood the off-chain order book with gas-bomb offers at negligible cost, causing aggregators/routers to include them. The attack is indefinitely repeatable with the same ratifier and offers because `take` always reverts before the consumption write, leaving `consumed` permanently at 0.

## Recommendation
Add a minimum gas guard before each `take` call to ensure the loop can always complete or fail gracefully:

```solidity
uint256 GAS_PER_TAKE = 500_000; // tune to worst-case take cost
if (gasleft() < GAS_PER_TAKE) break;
try IMidnight(MIDNIGHT).take{gas: GAS_PER_TAKE}(...) ...
```

Forwarding a bounded gas amount to each `take` call ensures that even if the ratifier burns all forwarded gas, sufficient gas remains for the rest of the transaction. Alternatively, the bundler can check `gasleft()` at the top of each loop iteration and `break` (not `revert`) if insufficient gas remains to safely proceed, converting a potential OOG revert into a graceful `OutOfOffers` revert.

## Proof of Concept

1. Deploy `MaliciousRatifier` implementing `isRatified(Offer, bytes)` that executes `assembly { for {} gt(gas(), 1000) {} }` then reverts.
2. Call `midnight.setIsAuthorized(address(maliciousRatifier), true, attacker)`.
3. Construct 3 `Take` structs with `offer.buy = true`, `offer.maker = attacker`, `offer.ratifier = address(maliciousRatifier)`, `offer.maxUnits = 1e18`, valid tick/expiry/market.
4. Call `supplyCollateralAndSellWithUnitsTarget` with `targetUnits = 1`, `takes = [bomb0, bomb1, bomb2, validOffer]`, supplying sufficient collateral and gas (e.g., 3M gas).
5. Observe: transaction reverts with OOG after 2–3 iterations; `validOffer` is never reached; collateral supply is rolled back; victim loses full gas cost.
6. Repeat indefinitely — `consumed[attacker][group]` remains 0 after each attempt.

### Citations

**File:** src/periphery/MidnightBundles.sol (L134-140)
```text
        for (uint256 i; i < collateralSupplies.length; i++) {
            address token = market.collateralParams[collateralSupplies[i].collateralIndex].token;
            pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
            forceApproveMax(token, MIDNIGHT);
            IMidnight(MIDNIGHT)
                .supplyCollateral(market, collateralSupplies[i].collateralIndex, collateralSupplies[i].assets, taker);
        }
```

**File:** src/periphery/MidnightBundles.sol (L144-161)
```text
        for (uint256 i; i < takes.length && filledUnits < targetUnits; i++) {
            require(takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
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
        }
```

**File:** src/Midnight.sol (L355-356)
```text
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

**File:** src/Midnight.sol (L366-373)
```text
        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```

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
