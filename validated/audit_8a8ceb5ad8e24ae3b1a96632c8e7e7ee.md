Looking at the actual code to validate each claim:

**Claim 1: `supplyCollateralAndSellWithUnitsTarget` loop with `try/catch`** [1](#0-0) 
Confirmed. The loop at lines 144–161 iterates `takes[]` with `try/catch` at line 152–160.

**Claim 2: Ratifier called before consumption write in `Midnight.take`** [2](#0-1) [3](#0-2) 
Confirmed. `isRatified` is called at line 356; the `consumed` write happens at lines 368/371 — after the ratifier call.

**Claim 3: `setIsAuthorized` is permissionless for self** [4](#0-3) 
Confirmed. `onBehalf == msg.sender` is allowed, so any attacker can authorize their own ratifier.

**Claim 4: `consumableUnits` returns non-zero after failed takes** [5](#0-4) 
Confirmed. Since `take` reverts before the `consumed` write, `consumed[attacker][group]` stays 0, so `consumableUnits` returns `offer.maxUnits - 0 = offer.maxUnits` on every iteration.

**Claim 5: `InconsistentSide` check is trivially satisfied** [6](#0-5) 
Confirmed. Attacker sets `offer.buy = true`.

All code-level claims check out. The EIP-150 gas mechanics (63/64 forwarding) are standard EVM behavior. The `try/catch` cannot refund gas already consumed inside the subcall. The attack preconditions (deploy ratifier, call `setIsAuthorized`, create offers) are all permissionless. The only external dependency is that the victim's `takes[]` array includes the attacker's offers — realistic given off-chain routing.

---

Audit Report

## Title
Gas griefing via malicious ratifier drains transaction gas in `supplyCollateralAndSellWithUnitsTarget` `takes[]` loop - (File: src/periphery/MidnightBundles.sol)

## Summary
An unprivileged attacker deploys a gas-bomb ratifier contract that consumes ~63/64 of forwarded gas before reverting, then creates `offer.buy=true` offers pointing to it. When a victim's `supplyCollateralAndSellWithUnitsTarget` call iterates a `takes[]` array containing these offers, the `try/catch` at line 152 catches each revert but cannot recover the gas consumed by the malicious ratifier. After 2–3 iterations the transaction runs out of gas and reverts entirely, rolling back the victim's collateral supply and wasting their full gas cost.

## Finding Description

**Exact code path:**

`MidnightBundles.supplyCollateralAndSellWithUnitsTarget` iterates `takes[]` with a `try/catch`:

```solidity
// MidnightBundles.sol lines 144–161
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
```

Inside `Midnight.take`, the ratifier is called **before** the consumption write:

```solidity
// Midnight.sol line 355–356
require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());

// Midnight.sol lines 367–373 — consumption write occurs AFTER ratifier call
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
} else {
    newConsumed = consumed[offer.maker][offer.group] += units;
    require(newConsumed <= offer.maxUnits, ConsumedUnits());
}
```

**Attacker-controlled inputs:**

1. Attacker deploys `MaliciousRatifier` whose `isRatified(...)` runs a tight loop consuming ~63/64 of forwarded gas, then reverts.
2. Attacker calls `midnight.setIsAuthorized(maliciousRatifier, true, attacker)` — permissionless (`onBehalf == msg.sender` branch).
3. Attacker creates N offers: `offer.buy = true`, `offer.maker = attacker`, `offer.ratifier = maliciousRatifier`, valid tick/expiry/market, `offer.maxUnits` set to a large value.
4. These offers pass `require(takes[i].offer.buy, InconsistentSide())` at line 145.

**Why `consumableUnits` does not skip the call:**

`ConsumableUnitsLib.consumableUnits` reads `consumed[offer.maker][offer.group]` from storage. Since `take` always reverts before the consumption write (line 371), `consumed` stays 0 and `consumableUnits` returns the full `offer.maxUnits` — a non-zero value — on every iteration, ensuring `unitsToTake > 0` and the `try` call is always made.

**EVM gas mechanics (EIP-150):**

Each `try IMidnight(MIDNIGHT).take(...)` forwards at most 63/64 of the caller's remaining gas. When `isRatified` burns all forwarded gas, `take` reverts and returns ~0 gas to `MidnightBundles`. After `catch {}`, `MidnightBundles` retains only ~1/64 of what it had before that iteration. After 2–3 gas-bomb iterations the remaining gas is negligible; the next loop overhead or subsequent valid `take` call triggers OOG, reverting the entire transaction.

**Why existing checks fail:**

- `InconsistentSide`: requires `offer.buy == true` — attacker satisfies this trivially.
- `RatifierUnauthorized`: attacker self-authorizes their own ratifier via the permissionless `setIsAuthorized`.
- `try/catch`: catches the revert from `take` but **cannot refund gas already consumed** by the malicious ratifier. Solidity `try/catch` is not a gas shield.
- `consumableUnits` returning 0: does not apply — the attacker's offers are never consumed because `take` reverts before the consumption write, so `consumableUnits` always returns non-zero.

## Impact Explanation
The victim's `supplyCollateralAndSellWithUnitsTarget` transaction reverts with OOG. Because OOG reverts the entire transaction, the collateral supply (lines 134–140) is also rolled back — no permanent state corruption occurs. The concrete impact is a low-cost, repeatable DoS of the sell-bundle flow: the victim cannot borrow against their collateral via the bundle even when valid offers exist later in `takes[]`, and wastes the full gas cost of the failed transaction on every attempt.

## Likelihood Explanation
All attacker preconditions are permissionless: deploying one gas-bomb ratifier (one-time cost), calling `setIsAuthorized` once, and creating ≥2 offers. The attacker does not need to front-run; they only need their offers to appear in the victim's routing-supplied `takes[]` array. Since routing is off-chain, an attacker can flood the off-chain order book with gas-bomb offers at negligible cost, causing aggregators/routers to include them. The attack is indefinitely repeatable with the same ratifier and offers because `take` always reverts before the consumption write, leaving `consumed` permanently at 0.

## Recommendation
Apply a gas stipend cap on the `try` call so that each `take` invocation cannot consume more than a bounded amount of gas, preserving enough gas for the remaining loop iterations and post-loop logic. Concretely, replace the bare `try IMidnight(MIDNIGHT).take(...)` with a low-level call that forwards a fixed gas budget (e.g., `gasleft() * 63 / 64` minus a safety margin, or a protocol-defined per-take gas cap). Alternatively, enforce a minimum gas check at the top of each loop iteration and revert with a clear error if insufficient gas remains, preventing silent OOG mid-loop. A complementary mitigation is to move the consumption write before the ratifier call in `Midnight.take`, so that repeated gas-bomb offers are skipped by `consumableUnits` after the first failed attempt — though this alone does not prevent the first gas-bomb hit.

## Proof of Concept
**Minimal Foundry test plan:**

1. Deploy `Midnight` and `MidnightBundles`.
2. Deploy `MaliciousRatifier` implementing `isRatified` as:
   ```solidity
   function isRatified(Offer memory, bytes memory) external returns (bytes4) {
       uint256 gas = gasleft();
       while (gasleft() > gas / 64) {} // burn ~63/64
       revert();
   }
   ```
3. Attacker calls `midnight.setIsAuthorized(address(maliciousRatifier), true, attacker)`.
4. Construct 3 `Take` structs with `offer.buy = true`, `offer.maker = attacker`, `offer.ratifier = address(maliciousRatifier)`, `offer.maxUnits = type(uint128).max`.
5. Call `bundles.supplyCollateralAndSellWithUnitsTarget(...)` with these 3 takes prepended before any legitimate takes, providing a realistic gas limit (e.g., 500,000 gas).
6. Observe the transaction reverts with OOG before reaching the `require(filledUnits == targetUnits, OutOfOffers())` check, and that the collateral supply is rolled back.

### Citations

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

**File:** src/Midnight.sol (L367-373)
```text
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
