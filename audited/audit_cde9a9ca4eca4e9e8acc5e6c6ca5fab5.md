I have read all the relevant files. Let me verify the key code paths before rendering a verdict.

All code paths are verified. The claim is accurate in every detail. Here is the verdict:

---

Audit Report

## Title
Maker callback can advance `consumed[maker][group]` mid-bundle, zeroing `consumableUnits` for subsequent same-group offers and reverting with `OutOfOffers` - (File: src/periphery/MidnightBundles.sol / src/periphery/ConsumableUnitsLib.sol)

## Summary
`ConsumableUnitsLib.consumableUnits` reads live on-chain `consumed[maker][group]` storage on every bundle loop iteration. Because `Midnight.take` increments the consumed counter and passes the cap check **before** invoking `offer.callback`, a malicious maker can call `setConsumed(group, type(uint256).max, maker)` inside that callback. Every subsequent `consumableUnits` call for any other offer sharing the same `(maker, group)` pair then returns 0, causing `unitsToTake = 0` for all remaining offers, and the bundle reverts with `OutOfOffers` after the taker has spent gas.

## Finding Description

**Root cause — TOCTOU on live `consumed` storage:**

`buyWithUnitsTargetAndWithdrawCollateral` (MidnightBundles.sol:71–88) computes `unitsToTake` on every iteration:

```solidity
uint256 unitsToTake = min(
    targetUnits - filledUnits,
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)  // line 77
);
```

`consumableUnits` (ConsumableUnitsLib.sol:14–17) reads live storage each time:

```solidity
uint256 consumed = IMidnight(midnight).consumed(offer.maker, offer.group);
if (offer.maxUnits > 0) {
    return offer.maxUnits.zeroFloorSub(consumed);
```

Inside `Midnight.take` (Midnight.sol:366–373), the consumed counter is incremented and the cap is checked **before** any callback fires:

```solidity
newConsumed = consumed[offer.maker][offer.group] += units;
require(newConsumed <= offer.maxUnits, ConsumedUnits());
```

For a sell offer, `sellerCallback` is resolved at Midnight.sol:421:

```solidity
address sellerCallback = offer.buy ? takerCallback : offer.callback;
```

The bundle always passes `takerCallback = address(0)` (MidnightBundles.sol:80), so `sellerCallback = offer.callback` — the maker's field — and it is invoked at Midnight.sol:458–473. `setConsumed` (Midnight.sol:723–728) has no reentrancy guard and is callable by anyone authorized for `onBehalf`:

```solidity
function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    require(amount >= consumed[onBehalf][group], AlreadyConsumed());
    consumed[onBehalf][group] = amount;
```

**Exploit flow:**

1. Maker creates two sell offers in the same group, each `maxUnits = 100`. Sets `offer.callback = MaliciousCallback` and calls `midnight.setIsAuthorized(MaliciousCallback, true, maker)`.
2. Taker calls `buyWithUnitsTargetAndWithdrawCollateral(targetUnits=200, ..., [offer0, offer1], ...)`.
3. **i=0**: `consumableUnits(offer0)` reads `consumed=0`, returns 100. `unitsToTake=100`. `take` increments `consumed` to 100, cap check passes. `MaliciousCallback.onSell` calls `midnight.setConsumed(group, type(uint256).max, maker)` → `consumed[maker][group] = type(uint256).max`. `take` returns successfully. `filledUnits = 100`.
4. **i=1**: `consumableUnits(offer1)` reads `consumed = type(uint256).max`, returns `zeroFloorSub(100, type(uint256).max) = 0`. `unitsToTake = 0`. `take(offer1, ..., 0, ...)` increments consumed by 0 (stays at `type(uint256).max`), then `require(type(uint256).max <= 100)` reverts with `ConsumedUnits()`. The `try/catch` swallows the revert. `filledUnits` stays at 100.
5. Loop ends. `require(100 == 200, OutOfOffers())` → **transaction reverts**.

**Why existing checks fail:**
- The `try/catch` at MidnightBundles.sol:79–85 only catches reverts from `take` itself; it cannot prevent the callback from mutating storage that affects the next loop iteration's `consumableUnits` call.
- The consumed cap check in `take` (Midnight.sol:372) fires before the callback, so the first take succeeds and the mutation is invisible to `take`'s own invariants.
- There is no snapshot or lock of `consumed` state at bundle entry.

The same attack applies identically to `supplyCollateralAndSellWithUnitsTarget` (MidnightBundles.sol:144–161), `buyWithAssetsTargetAndWithdrawCollateral` (MidnightBundles.sol:205–222), and `supplyCollateralAndSellWithAssetsTarget` (MidnightBundles.sol:282–301), all of which call `ConsumableUnitsLib.consumableUnits` inside their loops.

## Impact Explanation
Any taker who includes two or more offers from the same `(maker, group)` pair in a bundle function can have their transaction griefed to revert with `OutOfOffers`. The entire transaction reverts (no funds are lost), but the taker wastes gas and cannot complete the intended fill. The maker can repeat this indefinitely at zero cost beyond gas, constituting a persistent, low-cost DoS of all four bundle functions against any taker who includes their offers.

## Likelihood Explanation
All three preconditions are fully attacker-controlled and require no privileged access, no oracle manipulation, and no user mistake:
1. Maker sets `offer.callback` to a contract that calls `setConsumed(group, type(uint256).max, maker)` in `onSell`.
2. Maker pre-authorizes the callback contract via `setIsAuthorized`, or the callback contract address equals the maker address.
3. A taker includes at least two offers from the same `(maker, group)` pair in a bundle with `targetUnits` exceeding the first offer's remaining capacity.

The attack is repeatable at zero cost beyond gas and requires no victim mistake beyond including the maker's offers in a bundle.

## Recommendation
Snapshot the `consumed[maker][group]` value for each offer **before** the loop begins (or before each `take` call), and use the snapshot value in `consumableUnits` rather than reading live storage. Concretely, in `MidnightBundles`, record `consumed` for each `(maker, group)` pair at loop entry and pass it to a modified `consumableUnits` that accepts the pre-read value instead of re-reading storage. This eliminates the TOCTOU window between the `consumableUnits` check and the callback-induced mutation.

Alternatively, track units taken per `(maker, group)` pair within the bundle itself and compute remaining capacity from that local accounting rather than from live protocol storage.

## Proof of Concept
1. Deploy `MaliciousCallback` implementing `ISellCallback.onSell` that calls `midnight.setConsumed(group, type(uint256).max, maker)` and returns `CALLBACK_SUCCESS`.
2. As maker: create `offer0` and `offer1` with the same `group`, `maxUnits = 100`, `callback = MaliciousCallback`. Call `midnight.setIsAuthorized(MaliciousCallback, true, maker)`.
3. As taker: call `buyWithUnitsTargetAndWithdrawCollateral(targetUnits=200, ..., [offer0, offer1], ...)`.
4. Observe: `take(offer0, ..., 100)` succeeds; `MaliciousCallback.onSell` sets `consumed = type(uint256).max`; `consumableUnits(offer1)` returns 0; `take(offer1, ..., 0)` reverts with `ConsumedUnits()`; bundle reverts with `OutOfOffers()`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** src/periphery/MidnightBundles.sol (L71-88)
```text
        for (uint256 i; i < takes.length && filledUnits < targetUnits; i++) {
            require(!takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
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
        }

        require(filledUnits == targetUnits, OutOfOffers());
```

**File:** src/periphery/ConsumableUnitsLib.sol (L14-17)
```text
    function consumableUnits(address midnight, bytes32 id, Offer memory offer) internal view returns (uint256) {
        uint256 consumed = IMidnight(midnight).consumed(offer.maker, offer.group);
        if (offer.maxUnits > 0) {
            return offer.maxUnits.zeroFloorSub(consumed);
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

**File:** src/Midnight.sol (L420-421)
```text
        address buyerCallback = offer.buy ? offer.callback : takerCallback;
        address sellerCallback = offer.buy ? takerCallback : offer.callback;
```

**File:** src/Midnight.sol (L458-473)
```text
        if (sellerCallback != address(0)) {
            bytes memory sellerCallbackData = offer.buy ? takerCallbackData : offer.callbackData;
            require(
                ISellCallback(sellerCallback)
                    .onSell(
                        id,
                        offer.market,
                        sellerAssets,
                        units,
                        sellerPendingFeeDecrease,
                        seller,
                        receiver,
                        sellerCallbackData
                    ) == CALLBACK_SUCCESS,
                WrongSellCallbackReturnValue()
            );
```

**File:** src/Midnight.sol (L723-728)
```text
    function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        require(amount >= consumed[onBehalf][group], AlreadyConsumed());
        consumed[onBehalf][group] = amount;
        emit EventsLib.SetConsumed(msg.sender, group, amount, onBehalf);
    }
```

**File:** src/libraries/UtilsLib.sol (L22-26)
```text
    function zeroFloorSub(uint256 x, uint256 y) internal pure returns (uint256 z) {
        assembly {
            z := mul(gt(x, y), sub(x, y))
        }
    }
```
