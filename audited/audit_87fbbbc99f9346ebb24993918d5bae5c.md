Audit Report

## Title
Maker callback can call `setConsumed` mid-`take` to inflate consumed and grief taker multicall fills - (File: src/Midnight.sol)

## Summary
In `take`, `consumed[offer.maker][offer.group]` is incremented and bounds-checked before any external callback is invoked. For a buy offer, `offer.callback` is the maker's own address. Because `setConsumed` has no guard against being called during an active `take`, a maker-controlled callback can re-enter `setConsumed` to inflate `consumed` to `maxAssets` after the first fill, causing every subsequent fill of the same offer in the same `multicall` to revert with `ConsumedAssets`, rolling back the taker's entire atomic bundle.

## Finding Description
**Root cause:** `setConsumed` (lines 723–728) has two checks: authorization (`onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender]`) and non-decrease (`amount >= consumed[onBehalf][group]`). There is no transient lock, no reentrancy guard, and no check that it is not being called from within an active `take` execution. The `AlreadyConsumed` guard explicitly permits any upward adjustment, so inflating `consumed` to `maxAssets` during a callback passes both checks.

**Code path:**

1. `take` increments `consumed` and checks the cap at lines 367–373, before any callback: [1](#0-0) 

2. For `offer.buy == true`, `buyerCallback = offer.callback` (line 420) — the maker's own address: [2](#0-1) 

3. `IBuyCallback(buyerCallback).onBuy(...)` is invoked at lines 445–453 with no reentrancy protection on `setConsumed`: [3](#0-2) 

4. Inside `onBuy`, the malicious contract calls `midnight.setConsumed(group, maxAssets, maker)`. Authorization passes (`isAuthorized[maker][M]`). `AlreadyConsumed` passes (`maxAssets >= currentConsumed`). `consumed` is now `maxAssets`: [4](#0-3) 

5. The first `take` completes normally. The second `take` in the same `multicall` increments `consumed` again: `maxAssets + delta > maxAssets` → reverts `ConsumedAssets`. The entire `multicall` reverts because it propagates inner reverts: [5](#0-4) 

**Why existing checks fail:**
- `AlreadyConsumed` only prevents decreasing `consumed`; it explicitly allows any increase.
- There is no transient lock or reentrancy guard on `setConsumed` during `take`.
- The Certora `Consume.spec` rule `takeConsumedBoundedByMax` (lines 59–64) asserts `consumed <= maxAssets` post-`take`. Inflating `consumed` to exactly `maxAssets` satisfies this assertion, so the rule does not catch the inflation: [6](#0-5) 
- The rule `takeConsumedDelta` (lines 67–75) only applies to units mode (`offer.maxAssets == 0`) and does not cover the assets-mode exploit path: [7](#0-6) 

## Impact Explanation
A maker can grief any taker who attempts multiple partial fills of the same offer atomically via `multicall` or a bundler. The maker's callback inflates `consumed` to `maxAssets` after the first fill, causing every subsequent fill in the same transaction to revert. The taker's entire atomic bundle fails, wasting gas and preventing the intended fills. No direct theft of funds occurs, but atomic execution guarantees are broken for any taker relying on `multicall` for multi-fill bundles.

## Likelihood Explanation
Preconditions are trivial and permissionless: the maker deploys a callback contract and calls `setIsAuthorized(M, true, maker)` — both are normal protocol operations. No special privileges, oracle manipulation, or token owner action is required. The attack is deterministic and repeatable: the maker can deploy a fresh callback and offer with a new group for each target taker. The victim surface is any taker using `multicall` or a bundler for atomic multi-fill transactions.

## Recommendation
Add a transient lock in `take` that prevents `setConsumed` from being called for the same `(maker, group)` pair during an active `take` execution. Concretely, before the consumed increment at line 368, set a transient storage flag keyed on `(offer.maker, offer.group)`, and in `setConsumed`, revert if that flag is set. Clear the flag after the cap check (before callbacks) or after the full `take` completes. Alternatively, snapshot `consumed` before invoking callbacks and assert it has not changed after callbacks return, reverting if it has.

## Proof of Concept
**Minimal manual steps:**

1. Deploy malicious callback contract `M` implementing `IBuyCallback.onBuy` that calls `midnight.setConsumed(offer.group, offer.maxAssets, maker)` and returns `CALLBACK_SUCCESS`.
2. Maker calls `midnight.setIsAuthorized(M, true, maker)` to authorize `M` to call `setConsumed` on behalf of maker.
3. Maker creates a buy offer with `callback = M`, `maxAssets = X`, `group = G`, and a valid ratifier.
4. Taker calls `midnight.multicall([abi.encodeCall(take, (offer, ..., units1, ...)), abi.encodeCall(take, (offer, ..., units2, ...))])` where `units1` and `units2` are both nonzero and `buyerAssets1 + buyerAssets2 <= X`.
5. **Expected (buggy) behavior:** `take1` succeeds; inside `onBuy`, `M` sets `consumed[maker][G] = X`; `take2` increments `consumed` to `X + buyerAssets2 > X`, reverts `ConsumedAssets`; entire `multicall` reverts.
6. **Expected (correct) behavior:** Both fills succeed and `consumed[maker][G] = buyerAssets1 + buyerAssets2`.

### Citations

**File:** src/Midnight.sol (L211-220)
```text
    function multicall(bytes[] calldata calls) external {
        for (uint256 i = 0; i < calls.length; i++) {
            (bool success, bytes memory returnData) = address(this).delegatecall(calls[i]);
            if (!success) {
                assembly ("memory-safe") {
                    revert(add(returnData, 0x20), mload(returnData))
                }
            }
        }
    }
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

**File:** src/Midnight.sol (L445-453)
```text
        if (buyerCallback != address(0)) {
            bytes memory buyerCallbackData = offer.buy ? offer.callbackData : takerCallbackData;
            require(
                IBuyCallback(buyerCallback)
                    .onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
                == CALLBACK_SUCCESS,
                WrongBuyCallbackReturnValue()
            );
        }
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

**File:** certora/specs/Consume.spec (L59-64)
```text
rule takeConsumedBoundedByMax(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert offer.maxAssets > 0 => consumed(offer.maker, offer.group) <= offer.maxAssets;
    assert offer.maxAssets == 0 => consumed(offer.maker, offer.group) <= offer.maxUnits;
}
```

**File:** certora/specs/Consume.spec (L67-75)
```text
rule takeConsumedDelta(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxAssets == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert consumed(offer.maker, offer.group) == consumedBefore + units;
}
```
