Looking at the exact code in `src/ratifiers/EcrecoverRatifier.sol` lines 27-31:

```solidity
function cancelRoot(address maker, bytes32 root) external {
    require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
    isRootCanceled[maker][root] = true;
    emit CancelRoot(msg.sender, maker, root);
}
```

There is no guard checking `!isRootCanceled[maker][root]` before proceeding. The assignment is idempotent (`true = true`) but the event fires unconditionally every call. The maker is an explicitly listed valid attacker role. The path is fully reachable.

---

### Title
Double-cancellation of an already-canceled root emits duplicate `CancelRoot` events with no revert - (`File: src/ratifiers/EcrecoverRatifier.sol`)

### Summary
`cancelRoot` sets `isRootCanceled[maker][root] = true` and emits `CancelRoot` on every call without checking whether the root is already canceled. A maker (or any address authorized by the maker) can call `cancelRoot` repeatedly for the same `(maker, root)` pair, each time succeeding silently and emitting an additional `CancelRoot` event. The on-chain state is unchanged after the first call, but the event log accumulates duplicate entries.

### Finding Description
In `src/ratifiers/EcrecoverRatifier.sol` lines 27–31, `cancelRoot` performs only an authorization check and then unconditionally writes `isRootCanceled[maker][root] = true` and emits `CancelRoot`. There is no guard of the form `require(!isRootCanceled[maker][root], AlreadyCanceled())`. Because the maker is a valid caller (line 28: `maker == msg.sender`), the maker can invoke `cancelRoot(maker, root)` a second (or Nth) time after the root is already canceled. Each invocation passes the authorization check, the storage write is a no-op (`true → true`), but `emit CancelRoot(msg.sender, maker, root)` fires again. Off-chain indexers, subgraphs, or monitoring bots that count or react to `CancelRoot` events will observe multiple emissions for the same `(maker, root)` pair, potentially triggering duplicate alerts, incorrect cancel-count metrics, or replay-detection false positives.

### Impact Explanation
The concrete scoped impact is event log pollution: duplicate `CancelRoot(caller, maker, root)` entries appear in the transaction logs for the same logical cancellation. Any off-chain system that treats each event as a distinct cancellation action (e.g., an indexer building a cancel history, a monitoring bot alerting on cancellations, or a UI displaying "canceled at block X") will receive misleading data. On-chain state integrity is not affected.

### Likelihood Explanation
The precondition is trivially satisfiable: the maker simply calls `cancelRoot` twice with the same arguments. No special setup, no privileged role beyond being the maker, no race condition. It is repeatable an unlimited number of times at negligible cost (only gas). Any maker who has already canceled a root can trigger this at will.

### Recommendation
Add an idempotency guard at the top of `cancelRoot`:

```solidity
function cancelRoot(address maker, bytes32 root) external {
    require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
    require(!isRootCanceled[maker][root], AlreadyCanceled()); // add this
    isRootCanceled[maker][root] = true;
    emit CancelRoot(msg.sender, maker, root);
}
```

This ensures the event is emitted exactly once per `(maker, root)` pair and makes double-cancellation a clean revert rather than a silent duplicate emission.

### Proof of Concept

```solidity
function testDoubleCancelEmitsDuplicateEvent() public {
    bytes32 root = keccak256("root");

    // First cancellation — expected and legitimate
    vm.prank(lender);
    vm.expectEmit(true, true, true, true, address(ecrecoverRatifier));
    emit IEcrecoverRatifier.CancelRoot(lender, lender, root);
    ecrecoverRatifier.cancelRoot(lender, root);

    assertTrue(ecrecoverRatifier.isRootCanceled(lender, root));

    // Second cancellation — should revert but currently succeeds and emits again
    vm.prank(lender);
    vm.expectEmit(true, true, true, true, address(ecrecoverRatifier)); // this passes — duplicate event
    emit IEcrecoverRatifier.CancelRoot(lender, lender, root);
    ecrecoverRatifier.cancelRoot(lender, root); // no revert

    // State is unchanged (still true), but a second event was emitted
    assertTrue(ecrecoverRatifier.isRootCanceled(lender, root));
}
```

Expected assertions: second `cancelRoot` call does not revert, `isRootCanceled` remains `true` (state unchanged), and a second `CancelRoot` event is recorded in the logs — confirming duplicate event emission. [1](#0-0) [2](#0-1)

### Citations

**File:** src/ratifiers/EcrecoverRatifier.sol (L27-31)
```text
    function cancelRoot(address maker, bytes32 root) external {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootCanceled[maker][root] = true;
        emit CancelRoot(msg.sender, maker, root);
    }
```

**File:** src/ratifiers/interfaces/IEcrecoverRatifier.sol (L25-25)
```text
    event CancelRoot(address indexed caller, address indexed maker, bytes32 indexed root);
```
