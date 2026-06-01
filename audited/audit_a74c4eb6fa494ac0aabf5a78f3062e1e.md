### Title
`isRootCanceled` checked after expensive Merkle proof verification, wasting gas on canceled roots - (File: src/ratifiers/EcrecoverRatifier.sol)

### Summary
In `EcrecoverRatifier.isRatified`, the cheap `isRootCanceled` storage read is ordered after the expensive `HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof)` call. When a maker has canceled a root, every subsequent fill attempt against that root still pays for the full EIP-712 offer hash and Merkle traversal before reverting with `RootCanceled`. No funds are at risk, but gas is unnecessarily consumed by every caller.

### Finding Description
The execution path in `isRatified` is:

1. Decode `ratifierData` (cheap).
2. `HashLib.hashOffer(offer)` — hashes the full `Offer` struct including nested `Market` and `CollateralParams[]` via multiple `keccak256` calls.
3. Merkle traversal in `HashLib.isLeaf` — up to 20 `keccak256` calls (tree height capped at 20 by `offerTreeTypeHash`).
4. **Only then**: `require(!isRootCanceled[offer.maker][root], RootCanceled())` — a single cold/warm SLOAD.

Steps 2–3 are entirely wasted when the root is already canceled. The `isRootCanceled` mapping lookup at line 38 is independent of the Merkle proof result and can be moved to execute immediately after decoding `ratifierData`, before any hashing or traversal.

Attacker-controlled inputs: any caller (taker, bundler) who submits `ratifierData` referencing a canceled root triggers the wasted computation. The `MidnightBundles` periphery wraps `take` in a `try/catch` loop, meaning repeated failed attempts against a canceled root each pay the full hashing cost.

### Impact Explanation
Every call to `take` on an offer whose root has been canceled pays for `hashOffer` (multiple nested `keccak256` calls over the full `Offer`/`Market`/`CollateralParams[]` structs) plus up to 20 Merkle-node `keccak256` calls before reverting. This is unnecessary gas expenditure for the caller. No funds are lost or frozen; the revert is correct. Impact is strictly a non-critical gas inefficiency.

### Likelihood Explanation
Roots are canceled via `cancelRoot` as a normal maker workflow (e.g., to revoke a batch of offers). After cancellation, any taker or bundler that still holds stale `ratifierData` referencing the canceled root will trigger the wasted computation on every attempt. The `MidnightBundles` `try/catch` pattern makes repeated wasted calls especially easy to trigger.

### Recommendation
Move the `isRootCanceled` check immediately after decoding `ratifierData`, before calling `HashLib.isLeaf`:

```solidity
function isRatified(Offer memory offer, bytes memory ratifierData) external view returns (bytes32) {
    require(msg.sender == MIDNIGHT, NotMidnight());
    (Signature memory sig, bytes32 root, uint256 leafIndex, bytes32[] memory proof) =
        abi.decode(ratifierData, (Signature, bytes32, uint256, bytes32[]));
    require(!isRootCanceled[offer.maker][root], RootCanceled()); // moved up
    require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
    ...
}
```

This ensures the cheap SLOAD short-circuits before any hashing or Merkle traversal.

### Proof of Concept
```solidity
function testCanceledRootWastesGasOnMerkleVerification() public {
    Offer memory offer = makeOffer(lender);
    bytes32 _root = HashLib.hashOffer(offer);
    bytes memory ratifierData = buildRatifierData(_root, lender);

    // Maker cancels the root
    vm.prank(lender);
    ecrecoverRatifier.cancelRoot(lender, _root);

    // Measure gas: isRatified still runs hashOffer + isLeaf before reverting
    vm.prank(address(midnight));
    uint256 gasBefore = gasleft();
    try ecrecoverRatifier.isRatified(offer, ratifierData) {} catch {}
    uint256 gasUsed = gasBefore - gasleft();

    // Assert: gas used is significantly more than a bare SLOAD (~2100 gas)
    // With the fix, gasUsed should drop to ~2100 + decode overhead
    assertGt(gasUsed, 5000); // demonstrates wasted hashing cost
}
```

Expected assertion: `gasUsed` is substantially above a single SLOAD cost, confirming that `hashOffer` and Merkle traversal execute unnecessarily before the `RootCanceled` revert. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** src/ratifiers/EcrecoverRatifier.sol (L37-38)
```text
        require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
        require(!isRootCanceled[offer.maker][root], RootCanceled());
```

**File:** src/ratifiers/libraries/HashLib.sol (L53-63)
```text
    function isLeaf(bytes32 root, bytes32 leafHash, uint256 leafIndex, bytes32[] memory proof)
        internal
        pure
        returns (bool)
    {
        require(leafIndex >> proof.length == 0, LeafIndexOutOfRange());
        bytes32 currentHash = leafHash;
        for (uint256 i = 0; i < proof.length; i++) {
            currentHash = (leafIndex >> i) & 1 == 0 ? hashNode(currentHash, proof[i]) : hashNode(proof[i], currentHash);
        }
        return currentHash == root;
```

**File:** src/ratifiers/libraries/HashLib.sol (L118-138)
```text
    function hashOffer(Offer memory offer) internal pure returns (bytes32) {
        return keccak256(
            abi.encode(
                OFFER_TYPEHASH,
                hashMarket(offer.market),
                offer.buy,
                offer.maker,
                offer.start,
                offer.expiry,
                offer.tick,
                offer.group,
                offer.callback,
                keccak256(offer.callbackData),
                offer.receiverIfMakerIsSeller,
                offer.ratifier,
                offer.reduceOnly,
                offer.maxUnits,
                offer.maxAssets
            )
        );
    }
```
