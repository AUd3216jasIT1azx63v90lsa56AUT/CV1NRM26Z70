### Title
`HashLib.LeafIndexOutOfRange` Bypasses `RatifierFail` Wrapper in `Midnight.take` - (File: `src/ratifiers/libraries/HashLib.sol`)

### Summary
When a taker supplies a `leafIndex` where `leafIndex >> proof.length != 0` (e.g., `leafIndex=2` with `proof.length=1`), `HashLib.isLeaf` reverts with `LeafIndexOutOfRange()` before returning a boolean. Because `SetterRatifier.isRatified` calls `HashLib.isLeaf` as an internal function inside a `require`, the revert propagates out of `isRatified` carrying the `LeafIndexOutOfRange` selector. `Midnight.take` calls `isRatified` via an external call with no try/catch, so the `LeafIndexOutOfRange` revert bubbles through `take` to the caller — the `RatifierFail()` wrapper is never reached.

### Finding Description

**Exact code path:**

`HashLib.isLeaf` (internal library function) at line 58: [1](#0-0) 

```solidity
require(leafIndex >> proof.length == 0, LeafIndexOutOfRange());
```

With `leafIndex=2`, `proof.length=1`: `2 >> 1 = 1 ≠ 0` → reverts with `LeafIndexOutOfRange()`.

`SetterRatifier.isRatified` at line 34: [2](#0-1) 

```solidity
require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
```

Because `HashLib.isLeaf` is an **internal** call, its revert unwinds the stack before the `require` can evaluate the boolean condition. `InvalidProof()` is never thrown; `LeafIndexOutOfRange()` propagates out of `isRatified`.

`Midnight.take` at line 356: [3](#0-2) 

```solidity
require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

This is an **external** call with no `try/catch`. When `isRatified` reverts, the revert data (`LeafIndexOutOfRange`) is forwarded directly to the caller. `RatifierFail()` is never thrown.

**Attacker-controlled inputs:** `ratifierData = abi.encode(root, leafIndex, proof)` where `leafIndex >> proof.length != 0`. The taker fully controls `ratifierData`.

**Preconditions:** Maker has called `setIsRootRatified(maker, root, true)` and the offer is otherwise valid (not expired, maker authorized ratifier). These are normal operating conditions. [4](#0-3) 

**Existing checks:** The only guard is `require(leafIndex >> proof.length == 0, LeafIndexOutOfRange())` inside `HashLib.isLeaf`. There is no try/catch anywhere in the call chain to normalize the error.

### Impact Explanation

The error selector surfaced to callers of `take` is `LeafIndexOutOfRange()` (from `HashLib`) rather than `RatifierFail()` (from `IMidnight`). Integrators and smart-contract routers that catch `RatifierFail` to handle ratifier failures gracefully (e.g., to fall back to another offer or route) will not catch `LeafIndexOutOfRange`, causing an unexpected revert. Any automated system that relies on the invariant that all ratifier-path failures produce `RatifierFail` will malfunction when this input is supplied, potentially leaving funds inaccessible through that code path until the integrator is patched.

### Likelihood Explanation

The precondition (a ratified root and a valid offer) is the normal operating state for any `SetterRatifier`-gated offer. The trigger requires only that the taker supplies a `leafIndex` inconsistent with `proof.length`, which is a single attacker-controlled ABI-encoded field. It is trivially repeatable: any `leafIndex ≥ 2^proof.length` triggers it. No privileged access, oracle manipulation, or token owner action is required.

### Recommendation

Wrap the `HashLib.isLeaf` call in `SetterRatifier.isRatified` with a try/catch or pre-validate `leafIndex` before calling `isLeaf`, so that any revert from `isLeaf` is caught and re-thrown as `InvalidProof()`:

```solidity
// Option A: pre-validate
require(leafIndex >> proof.length == 0, InvalidProof());
require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
```

Alternatively, `Midnight.take` could wrap the external ratifier call in a try/catch and always throw `RatifierFail()` on any revert, normalizing all ratifier errors at the protocol boundary.

### Proof of Concept

```solidity
function testLeafIndexOutOfRangePropagatesThroughTake() public {
    // Setup: maker ratifies a root for a single-leaf tree (proof.length == 0 for a single leaf)
    Offer memory offer = makeOffer(maker);
    bytes32 leafHash = HashLib.hashOffer(offer);
    // root == leafHash for a single-leaf tree
    bytes32 root = leafHash;

    vm.prank(maker);
    midnight.setIsAuthorized(address(setterRatifier), true, maker);
    vm.prank(maker);
    setterRatifier.setIsRootRatified(maker, root, true);

    // Taker supplies leafIndex=2 with proof.length=1 (2 >> 1 = 1 != 0)
    bytes32[] memory proof = new bytes32[](1);
    proof[0] = bytes32(0);

    // Assert: revert selector is LeafIndexOutOfRange, NOT RatifierFail
    vm.expectRevert(HashLib.LeafIndexOutOfRange.selector);
    vm.prank(taker);
    midnight.take(
        offer,
        abi.encode(root, uint256(2), proof),
        0,
        taker,
        taker,
        address(0),
        hex""
    );

    // Also test leafIndex=type(uint256).max with proof.length=1
    vm.expectRevert(HashLib.LeafIndexOutOfRange.selector);
    vm.prank(taker);
    midnight.take(
        offer,
        abi.encode(root, type(uint256).max, proof),
        0,
        taker,
        taker,
        address(0),
        hex""
    );
}
```

Expected assertion: both calls revert with `LeafIndexOutOfRange.selector`, confirming that `RatifierFail` is never surfaced and the invariant is broken.

### Citations

**File:** src/ratifiers/libraries/HashLib.sol (L58-58)
```text
        require(leafIndex >> proof.length == 0, LeafIndexOutOfRange());
```

**File:** src/ratifiers/SetterRatifier.sol (L24-27)
```text
    function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootRatified[maker][root] = newIsRootRatified;
        emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
```

**File:** src/ratifiers/SetterRatifier.sol (L34-34)
```text
        require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
```

**File:** src/Midnight.sol (L356-356)
```text
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```
