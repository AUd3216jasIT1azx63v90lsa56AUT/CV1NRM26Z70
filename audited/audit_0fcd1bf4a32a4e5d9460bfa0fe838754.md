Audit Report

## Title
Gas griefing via malicious ratifier drains transaction gas in `supplyCollateralAndSellWithUnitsTarget` `takes[]` loop - (File: src/periphery/MidnightBundles.sol)

## Summary
An unprivileged attacker deploys a gas-bomb ratifier that consumes ~63/64 of forwarded gas before reverting, self-authorizes it via the permissionless `setIsAuthorized` path, and creates `offer.buy=true` offers pointing to it. When a victim's `supplyCollateralAndSellWithUnitsTarget` call iterates a `takes[]` array containing these offers, the `try/catch` at line 152 catches each revert but cannot recover the gas consumed inside the subcall. After 2–3 iterations the transaction runs out of gas and reverts entirely, rolling back the victim's collateral supply and wasting their full gas cost.

## Finding Description

**Loop with `try/catch` — confirmed at lines 144–161:** [1](#0-0) 

The loop iterates `takes[]` and calls `IMidnight(MIDNIGHT).take(...)` inside a `try/catch`. A revert inside `take` is silently swallowed, but gas already consumed by the callee is not refunded.

**Ratifier called before consumption write — confirmed at lines 355–373 of `Midnight.sol`:** [2](#0-1) [3](#0-2) 

`IRatifier(offer.ratifier).isRatified(...)` is called at line 356. The `consumed[offer.maker][offer.group]` write happens at lines 368/371 — after the ratifier call. When the ratifier reverts, `take` reverts before the consumption write, leaving `consumed` permanently at 0.

**`consumableUnits` returns non-zero on every iteration — confirmed:** [4](#0-3) 

`consumableUnits` reads `consumed[offer.maker][offer.group]` from storage. Because `take` always reverts before the write, `consumed` stays 0 and `consumableUnits` returns `offer.maxUnits - 0 = offer.maxUnits` on every iteration, ensuring `unitsToTake > 0` and the `try` call is always made.

**`InconsistentSide` check trivially satisfied:** [5](#0-4) 

`require(takes[i].offer.buy, InconsistentSide())` — attacker sets `offer.buy = true`.

**`setIsAuthorized` permissionless for self:** [6](#0-5) 

The `isAuthorized` mapping is writable by any account for itself (`onBehalf == msg.sender` branch), so the attacker can authorize their own malicious ratifier without any privilege.

**EVM gas mechanics:** Each `try IMidnight(MIDNIGHT).take(...)` forwards at most 63/64 of the caller's remaining gas (EIP-150). When `isRatified` burns all forwarded gas, `take` reverts and returns ~0 gas to `MidnightBundles`. After `catch {}`, `MidnightBundles` retains only ~1/64 of what it had before that iteration. After 2–3 gas-bomb iterations the remaining gas is negligible; the next loop overhead triggers OOG, reverting the entire transaction.

**Why existing checks fail:**
- `InconsistentSide`: satisfied by setting `offer.buy = true`.
- `RatifierUnauthorized`: bypassed by self-authorizing the malicious ratifier.
- `try/catch`: catches the revert but cannot refund gas already consumed inside the subcall.
- `consumableUnits == 0` skip: does not apply — `consumed` stays 0 because `take` reverts before the write.

## Impact Explanation
The victim's `supplyCollateralAndSellWithUnitsTarget` transaction reverts with OOG. Because OOG reverts the entire transaction, the collateral supply (lines 134–140) is also rolled back — no permanent state corruption occurs. The concrete impact is a low-cost, repeatable DoS of the sell-bundle flow: the victim cannot borrow against their collateral via the bundle even when valid offers exist later in `takes[]`, and wastes the full gas cost of the failed transaction on every attempt. [7](#0-6) 

## Likelihood Explanation
All attacker preconditions are permissionless: deploying one gas-bomb ratifier (one-time cost), calling `setIsAuthorized` once, and creating ≥2 offers. The attacker does not need to front-run; they only need their offers to appear in the victim's routing-supplied `takes[]` array. Since routing is off-chain, an attacker can flood the off-chain order book with gas-bomb offers at negligible cost, causing aggregators/routers to include them. The attack is indefinitely repeatable with the same ratifier and offers because `take` always reverts before the consumption write, leaving `consumed` permanently at 0.

## Recommendation
1. **Gas-cap the subcall**: Forward a bounded gas amount to each `take` call, e.g. `try IMidnight(MIDNIGHT).take{gas: SAFE_GAS_LIMIT}(...)`. Choose `SAFE_GAS_LIMIT` to be sufficient for a legitimate take but small enough that burning it all does not OOG the outer loop.
2. **Minimum gas guard per iteration**: Before each `try`, `require(gasleft() >= MIN_GAS_PER_ITERATION, InsufficientGas())` to fail fast with a clear error rather than OOG mid-loop.
3. **Off-chain routing filter**: Routing software should validate that `offer.ratifier` is a known-safe ratifier (e.g., `EcrecoverRatifier` or `SetterRatifier`) before including an offer in `takes[]`.

## Proof of Concept

```solidity
// 1. Deploy gas-bomb ratifier
contract MaliciousRatifier {
    function isRatified(Offer calldata, bytes calldata) external returns (bytes4) {
        uint256 i;
        while (gasleft() > 600) { unchecked { i++; } } // burn ~63/64 of forwarded gas
        revert();
    }
}

// 2. Attacker self-authorizes it
midnight.setIsAuthorized(address(maliciousRatifier), true, attacker);

// 3. Attacker creates N offers: offer.buy=true, offer.ratifier=maliciousRatifier,
//    offer.maxUnits = type(uint128).max, valid tick/expiry/market

// 4. Victim (or routing software) calls:
bundles.supplyCollateralAndSellWithUnitsTarget(
    targetUnits, minSellerAssets, taker, receiver,
    collateralSupplies,
    [attackerOffer1, attackerOffer2, attackerOffer3, ...legitimateOffers],
    0, address(0)
);
// Expected: OOG revert after 2-3 gas-bomb iterations.
// Collateral supply is rolled back. Victim loses full gas cost.
// Attacker repeats indefinitely at negligible cost.
```

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

**File:** src/Midnight.sol (L192-192)
```text
    mapping(address authorizer => mapping(address authorized => bool)) public isAuthorized;
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

**File:** src/periphery/ConsumableUnitsLib.sol (L14-18)
```text
    function consumableUnits(address midnight, bytes32 id, Offer memory offer) internal view returns (uint256) {
        uint256 consumed = IMidnight(midnight).consumed(offer.maker, offer.group);
        if (offer.maxUnits > 0) {
            return offer.maxUnits.zeroFloorSub(consumed);
        } else if (offer.buy) {
```
