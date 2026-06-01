Now I have all the code I need. Let me trace the exact execution path.

### Title
No-op take bypasses consumed-cap cancellation to trigger maker callback — (`src/Midnight.sol` / `src/ratifiers/SetterRatifier.sol`)

### Summary
When a maker cancels an offer by calling `setConsumed(G, N, maker)` (setting consumed to `maxUnits`), the `take` function's cap check uses `<=` rather than `<`, so a call with `units=0` produces `newConsumed = N + 0 = N` and `require(N <= N)` passes. Because `SetterRatifier` state is unaffected by `setConsumed`, the ratifier check also passes, and the maker's `onBuy`/`onSell` callback is unconditionally invoked with `units=0, buyerAssets=0, sellerAssets=0`. Any unprivileged taker can repeat this indefinitely.

### Finding Description
**Code path:**

`src/Midnight.sol` `take()` lines 366–373 — the consumed accounting block:

```solidity
} else {
    newConsumed = consumed[offer.maker][offer.group] += units;   // += 0 → stays at N
    require(newConsumed <= offer.maxUnits, ConsumedUnits());     // N <= N → passes
}
``` [1](#0-0) 

`src/ratifiers/SetterRatifier.sol` `isRatified()` lines 30–36 — only checks `isRootRatified[maker][root]`, which is never cleared by `setConsumed`:

```solidity
require(isRootRatified[offer.maker][root], NotRatified());
return CALLBACK_SUCCESS;
``` [2](#0-1) 

`src/Midnight.sol` lines 445–453 — callback invoked unconditionally when `offer.callback != address(0)`, regardless of `units`:

```solidity
if (buyerCallback != address(0)) {
    require(
        IBuyCallback(buyerCallback)
            .onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
        == CALLBACK_SUCCESS, ...
    );
}
``` [3](#0-2) 

**Exploit flow:**

1. Maker calls `setIsRootRatified(maker, R, true)` where R is a Merkle root containing offer O (`group=G, maxUnits=N, maxAssets=0, callback=<maker_callback>`).
2. Maker (or authorized operator) calls `setConsumed(G, N, maker)` intending to cancel O. [4](#0-3) 
3. Attacker (any address ≠ maker) calls `take(O, ratifierData, 0, attacker, attacker, address(0), hex"")`.
4. `isRatified` passes — `isRootRatified[maker][R]` is still `true`. [5](#0-4) 
5. `consumed[maker][G] += 0` → `newConsumed = N`; `require(N <= N)` passes. [6](#0-5) 
6. `buyerAssets = 0`, `sellerAssets = 0` (computed from `units=0`). [7](#0-6) 
7. `onBuy(id, market, 0, 0, 0, buyer, callbackData)` is called on the maker's callback contract. [8](#0-7) 
8. Token transfers are no-ops (`safeTransferFrom(..., 0)`). [9](#0-8) 

**Why existing checks fail:** There is no `require(units > 0)` guard anywhere in `take`. The cap check is `<=` not `<`, so `consumed == maxUnits` with `units == 0` is explicitly allowed. The Certora spec itself acknowledges this gap — rule `fullyConsumedOfferRevertsOnNonTrivialTake` only asserts `units == 0` after a successful take on a fully-consumed offer, but does not assert that callbacks are not triggered. [10](#0-9) 

### Impact Explanation
A cancelled offer (consumed set to `maxUnits` via `setConsumed`) can still have its maker callback triggered an unlimited number of times via no-op takes. Any maker callback that does not explicitly guard against `units == 0` (e.g., one that updates internal accounting, mints tokens, emits state-changing events, or interacts with other protocols) will execute unintended logic. This directly violates the core invariant: "offers cannot be replayed, overfilled, reused, or filled after cancel/deadline." [11](#0-10) 

### Likelihood Explanation
**Preconditions:** Maker must have set a non-zero `offer.callback` in the offer and must have used `setConsumed` (rather than `setIsRootRatified(..., false)`) as the cancellation mechanism. Both are normal, documented usage patterns. **Feasibility:** Any unprivileged address can be the taker; no special permissions required beyond `taker == msg.sender`. **Repeatability:** The attack is repeatable indefinitely — each call with `units=0` passes all checks and re-triggers the callback, since `consumed` does not increase. [12](#0-11) 

### Recommendation
Add a guard at the top of `take` requiring `units > 0`, or change the cap check from `<=` to `<` so that `consumed == maxUnits` causes an immediate revert regardless of `units`:

```solidity
// Option A: explicit zero-unit guard
require(units > 0, ZeroUnits());

// Option B: strict cap check
require(newConsumed < offer.maxUnits, ConsumedUnits());  // was <=
```

Option A is cleaner and also prevents zero-unit takes from triggering callbacks in all other scenarios. Option B alone is insufficient if `units=0` and `consumed < maxUnits` (the callback would still fire with zero amounts). Both options together are the safest fix. [1](#0-0) 

### Proof of Concept
```solidity
// Foundry unit test
function testCancelledOfferCallbackTriggeredByNoOpTake() public {
    // 1. Deploy a mock callback that counts invocations
    MockBuyCallback cb = new MockBuyCallback();

    // 2. Build offer O: buy=true, group=G, maxUnits=N, callback=address(cb)
    Offer memory offer = buildOffer(maker, G, N, address(cb));

    // 3. Maker ratifies root R = hashOffer(offer)
    bytes32 root = HashLib.hashOffer(offer);
    vm.prank(maker);
    setterRatifier.setIsRootRatified(maker, root, true);

    // 4. Maker cancels by setting consumed to maxUnits
    vm.prank(maker);
    midnight.setConsumed(G, N, maker);
    assertEq(midnight.consumed(maker, G), N);

    // 5. Attacker calls take with units=0
    bytes memory ratifierData = abi.encode(root, 0, new bytes32[](0));
    vm.prank(attacker);
    midnight.take(offer, ratifierData, 0, attacker, attacker, address(0), hex"");

    // 6. Assert callback was invoked despite cancellation
    assertEq(cb.callCount(), 1, "callback triggered on cancelled offer");

    // 7. Assert consumed unchanged (still N, not N+1)
    assertEq(midnight.consumed(maker, G), N);

    // 8. Repeat to show griefing is unbounded
    vm.prank(attacker);
    midnight.take(offer, ratifierData, 0, attacker, attacker, address(0), hex"");
    assertEq(cb.callCount(), 2, "callback triggered again");
}
```

Expected assertions: both `cb.callCount()` assertions pass, demonstrating the callback fires on a fully-consumed (cancelled) offer. [13](#0-12)

### Citations

**File:** src/Midnight.sol (L346-356)
```text
        require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
        bytes32 id = touchMarket(offer.market);
        MarketState storage _marketState = marketState[id];
        require(_marketState.lossFactor < type(uint128).max, MarketLossFactorMaxedOut());
        require(UtilsLib.atMostOneNonZero(offer.maxAssets, offer.maxUnits), MultipleNonZero());
        require(offer.tick % _marketState.tickSpacing == 0, TickNotAccessible());
        require(block.timestamp >= offer.start, OfferNotStarted());
        require(block.timestamp <= offer.expiry, OfferExpired());
        require(offer.maker != taker, SelfTake());
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

**File:** src/Midnight.sol (L363-364)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
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

**File:** src/Midnight.sol (L444-453)
```text
        bool wasLocked = UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, true);
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

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
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

**File:** src/ratifiers/SetterRatifier.sol (L30-37)
```text
    function isRatified(Offer memory offer, bytes memory ratifierData) external view returns (bytes32) {
        require(msg.sender == MIDNIGHT, NotMidnight());
        (bytes32 root, uint256 leafIndex, bytes32[] memory proof) =
            abi.decode(ratifierData, (bytes32, uint256, bytes32[]));
        require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
        require(isRootRatified[offer.maker][root], NotRatified());
        return CALLBACK_SUCCESS;
    }
```

**File:** certora/specs/Consume.spec (L99-111)
```text
/// A fully-consumed offer in units mode only allows no-op takes.
rule fullyConsumedOfferRevertsOnNonTrivialTake(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxAssets == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    require offer.maxUnits > 0 && consumedBefore >= offer.maxUnits, "assume the offer is fully consumed";

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    // If take does not revert, its input has to be zero.
    assert units == 0;
}
```
