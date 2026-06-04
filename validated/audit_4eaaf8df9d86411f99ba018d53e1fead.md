### Title
`setConsumed` Emits Misleading `SetConsumed` Event on Already-Cancelled/Fully-Consumed Offer Group — (File: src/Midnight.sol)

---

### Summary

The `setConsumed` function in `Midnight.sol` is the offer-cancellation mechanism for offer groups. Its only guard is `require(amount >= consumed[onBehalf][group], AlreadyConsumed())`, which prevents lowering the consumed value but does **not** prevent re-invoking cancellation when the group is already at `type(uint256).max`. A maker (or any authorized account) can call `setConsumed(group, type(uint256).max, onBehalf)` on a group that is already fully consumed or already cancelled, causing a spurious `SetConsumed` event to be emitted with no state change. Off-chain systems that rely on this event to track offer lifecycle will misinterpret the event as a new cancellation action.

---

### Finding Description

The cancellation function is:

```solidity
/// @dev Passing type(uint256).max cancels all offers in the group (and never reverts).
function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    require(amount >= consumed[onBehalf][group], AlreadyConsumed());
    consumed[onBehalf][group] = amount;
    emit EventsLib.SetConsumed(msg.sender, group, amount, onBehalf);
}
``` [1](#0-0) 

The check `amount >= consumed[onBehalf][group]` guards against decreasing the consumed counter, but when `consumed[onBehalf][group]` is already `type(uint256).max` (the terminal/cancelled state), passing `type(uint256).max` satisfies the check (`max >= max`), the assignment is a no-op, and the `SetConsumed` event is emitted anyway. [2](#0-1) 

Two concrete paths reach this terminal state:

1. **Already cancelled**: A maker calls `setConsumed(group, type(uint256).max, onBehalf)` once to cancel. A second identical call succeeds silently and emits a duplicate `SetConsumed` event.
2. **Already fully consumed via `take`**: If `maxUnits` or `maxAssets` is set to `type(uint256).max` and takers exhaust the group, `consumed` reaches `type(uint256).max`. A subsequent `setConsumed(group, type(uint256).max, onBehalf)` call emits a `SetConsumed` event suggesting the maker cancelled offers that were actually fully filled. [3](#0-2) 

This is the direct analog to the 1inch `LimitOrderProtocol` bug: in that protocol, `cancelOrder` could be called on an order whose `_remaining[orderHash] == 1` (fully filled sentinel), emitting an incorrect `OrderCancelled` event. Here, `setConsumed` can be called on a group whose `consumed == type(uint256).max` (fully consumed/cancelled sentinel), emitting a spurious `SetConsumed` event.

---

### Impact Explanation

The on-chain state is unchanged (consumed stays at `type(uint256).max`). However:

- Off-chain indexers, UIs, and analytics systems that consume `SetConsumed` events to reconstruct offer lifecycle will record a new cancellation action for a group that was already in a terminal state.
- In the "already filled" path, the event falsely signals that the maker cancelled their offers, when in reality they were fully taken. This corrupts offer-fill vs. offer-cancel attribution in any off-chain system.
- Automated systems (e.g., bots or dashboards) that trigger actions on `SetConsumed` events could be manipulated into performing redundant or incorrect operations.

---

### Likelihood Explanation

The trigger requires no privileges beyond being the `onBehalf` address or an authorized account for it. Any maker can reproduce this at zero cost by calling `setConsumed` twice with `type(uint256).max`. The "never reverts" NatSpec comment on the function even implicitly documents this behavior, making it likely to be exercised in practice. [4](#0-3) 

---

### Recommendation

Add a guard that prevents calling `setConsumed` when the group is already at the terminal value:

```solidity
function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    require(amount >= consumed[onBehalf][group], AlreadyConsumed());
    require(consumed[onBehalf][group] != type(uint256).max, "LOP: already cancelled or fully consumed");
    consumed[onBehalf][group] = amount;
    emit EventsLib.SetConsumed(msg.sender, group, amount, onBehalf);
}
```

This mirrors the 1inch recommendation (`require(_remaining[orderHash] != 1, "LOP: already filled")`) and ensures the event is only emitted when a genuine state transition occurs.

---

### Proof of Concept

**Scenario A — Cancel of already-cancelled group:**

1. Maker calls `setConsumed(group, type(uint256).max, maker)` → `consumed[maker][group]` becomes `type(uint256).max`, `SetConsumed` emitted (legitimate cancellation).
2. Maker calls `setConsumed(group, type(uint256).max, maker)` again → check passes (`max >= max`), state unchanged, **spurious `SetConsumed` event emitted**.
3. Off-chain indexer records two cancellation events for the same group.

**Scenario B — Cancel of already-filled group:**

1. Maker creates an offer with `offer.maxUnits = type(uint256).max` and `offer.group = G`.
2. Takers call `take(...)` repeatedly; `consumed[maker][G]` accumulates to `type(uint256).max`.
3. Maker calls `setConsumed(G, type(uint256).max, maker)` → check passes, state unchanged, **`SetConsumed` event emitted**.
4. Off-chain system records the group as "cancelled by maker" when it was actually "fully filled by takers". [5](#0-4)

### Citations

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

**File:** src/Midnight.sol (L722-728)
```text
    /// @dev Passing type(uint256).max cancels all offers in the group (and never reverts).
    function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        require(amount >= consumed[onBehalf][group], AlreadyConsumed());
        consumed[onBehalf][group] = amount;
        emit EventsLib.SetConsumed(msg.sender, group, amount, onBehalf);
    }
```

**File:** src/libraries/EventsLib.sol (L28-28)
```text
    event SetConsumed(address indexed caller, bytes32 indexed group, uint256 amount, address indexed onBehalf);
```
