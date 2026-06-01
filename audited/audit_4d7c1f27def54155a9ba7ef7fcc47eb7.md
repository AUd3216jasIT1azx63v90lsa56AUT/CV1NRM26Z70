Looking at `EcrecoverRatifier.sol` lines 33–46 and `HashLib.sol` lines 53–64, I can now analyze the ordering issue precisely.

### Title
Check Ordering Causes Unnecessary O(height) Merkle Proof Computation Before Canceled-Root Guard - (File: src/ratifiers/EcrecoverRatifier.sol)

### Summary
In `EcrecoverRatifier.isRatified`, the O(height) Merkle proof verification via `HashLib.isLeaf` executes at line 37 before the cheap `isRootCanceled` SLOAD at line 38. When a maker has canceled a root and a taker submits a valid proof for that root, up to 20 `keccak256` iterations run unnecessarily before the function reverts with `RootCanceled`. The fix is a trivial reordering of the two `require` statements.

### Finding Description
The execution path in `EcrecoverRatifier.isRatified` is:

```
line 34: require(msg.sender == MIDNIGHT)          // gate: only Midnight
line 35-36: abi.decode(ratifierData, ...)          // decode sig, root, leafIndex, proof
line 37: require(HashLib.isLeaf(...), InvalidProof()) // O(proof.length) keccak256 loop
line 38: require(!isRootCanceled[offer.maker][root])  // single SLOAD
```

`HashLib.isLeaf` iterates `proof.length` times (up to 20, capped by `offerTreeTypeHash`), each iteration calling `hashNode` which executes one `keccak256` over 64 bytes. This runs in full before the `isRootCanceled` mapping lookup, which is a single cold or warm SLOAD.

**Exploit flow:**
1. Maker creates a Merkle tree of height 20 and signs the root.
2. Maker calls `cancelRoot(maker, root)` → `isRootCanceled[maker][root] = true`.
3. Taker (who holds a valid proof for an offer in that tree) calls `midnight.take(offer, ratifierData, ...)` with `proof.length == 20`.
4. Midnight calls `ecrecoverRatifier.isRatified(offer, ratifierData)`.
5. Line 37 runs 20 `keccak256` iterations (proof is valid, so `isLeaf` returns `true`).
6. Line 38 reads `isRootCanceled[maker][root] == true` and reverts with `RootCanceled`.

The taker's transaction reverts correctly, but ~20 × ~30 gas (plus memory and loop overhead) is consumed on proof verification that was entirely avoidable by checking cancellation first.

### Impact Explanation
Unnecessary gas is consumed on every attempt to fill an offer whose root has been canceled, proportional to tree height (up to 20 keccak256 iterations). No funds are lost, no state is corrupted, and the revert is correct. The impact is strictly the wasted computation cost borne by the taker, matching the scoped "unnecessary gas cost" classification.

### Likelihood Explanation
Preconditions are easily met in normal protocol use: a maker cancels a root (a routine operation), and any taker who previously obtained a valid proof for that root and attempts to fill the offer triggers the wasted computation. The scenario is repeatable on every such fill attempt and requires no special privileges — any taker with a valid proof can trigger it.

### Recommendation
Swap lines 37 and 38 so the cheap SLOAD is checked first:

```solidity
require(!isRootCanceled[offer.maker][root], RootCanceled());   // SLOAD first
require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof()); // then O(height)
```

This eliminates all Merkle computation for canceled roots at the cost of one storage read.

### Proof of Concept
```solidity
function testCanceledRootWastesGasOnMerkleVerification() public {
    // Build a height-20 Merkle tree (only need root + valid proof path)
    Offer memory offer = makeOffer(lender);
    bytes32 leafHash = HashLib.hashOffer(offer);

    // Construct a valid 20-element proof and compute the root bottom-up
    bytes32[] memory proof = new bytes32[](20);
    bytes32 current = leafHash;
    for (uint256 i = 0; i < 20; i++) {
        proof[i] = keccak256(abi.encode(i)); // arbitrary siblings
        current = HashLib.hashNode(current, proof[i]); // leafIndex == 0, always left
    }
    bytes32 root = current;

    // Maker cancels the root
    vm.prank(lender);
    ecrecoverRatifier.cancelRoot(lender, root);
    assertTrue(ecrecoverRatifier.isRootCanceled(lender, root));

    // Build ratifier data with valid proof for canceled root
    Signature memory sig = signature(root, privateKey[lender], address(ecrecoverRatifier), 0);
    bytes memory ratifierData = abi.encode(sig, root, uint256(0), proof);

    // Taker triggers isRatified via Midnight; measure gas
    uint256 gasBefore = gasleft();
    vm.prank(address(midnight));
    vm.expectRevert(IEcrecoverRatifier.RootCanceled.selector);
    ecrecoverRatifier.isRatified(offer, ratifierData);
    uint256 gasUsed = gasBefore - gasleft();

    // Assert: gas used is significantly higher than a bare SLOAD (~2100 cold)
    // because 20 keccak256 iterations ran before the cancellation check
    assertGt(gasUsed, 5000); // well above a single SLOAD cost
}
```

**Expected assertions:** The call reverts with `RootCanceled`; `gasUsed` is measurably higher than the cost of a single SLOAD, confirming that the full 20-iteration Merkle loop executed before the cancellation guard.

---

**Code references:**

`isRatified` check ordering — `isLeaf` at line 37 before `isRootCanceled` at line 38: [1](#0-0) 

`HashLib.isLeaf` O(height) loop (up to 20 iterations): [2](#0-1) 

`offerTreeTypeHash` caps height at 20: [3](#0-2) 

`cancelRoot` sets the flag that is checked too late: [4](#0-3)

### Citations

**File:** src/ratifiers/EcrecoverRatifier.sol (L27-31)
```text
    function cancelRoot(address maker, bytes32 root) external {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootCanceled[maker][root] = true;
        emit CancelRoot(msg.sender, maker, root);
    }
```

**File:** src/ratifiers/EcrecoverRatifier.sol (L37-38)
```text
        require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
        require(!isRootCanceled[offer.maker][root], RootCanceled());
```

**File:** src/ratifiers/libraries/HashLib.sol (L22-47)
```text
    function offerTreeTypeHash(uint256 height) internal pure returns (bytes32) {
        if (height <= 10) {
            if (height == 0) return 0x2b9ee710e1977dfc5778fe18c905ccc1d9e144baf3ba83be732d4da65ecb73e3;
            if (height == 1) return 0x3cc16189b92a85898f1d5c6e87282c8ded7c1c93b2323d5e85ae10c5f4b2b220;
            if (height == 2) return 0x6de37d3e570afa293a8107d4b6b1d9547616c04f42164d009c89194787b2ffa6;
            if (height == 3) return 0xba3ea2ddfbf40a906fcd1b9506dbd344c062e8dcba8b5c902ceb13339f45a358;
            if (height == 4) return 0xe5faa865e93bc1b7b8fdf91980f54682d649683b014edd6c54b642f5a0c96977;
            if (height == 5) return 0xeda50f61dd2a827c6ff9fbfcd54335628dcaa78aaa4f2d118c60886219cdce2b;
            if (height == 6) return 0x54e2c9cc40cdc0e9ad530cf2be298f952f57af2b18b02f88274a9bbab359d23a;
            if (height == 7) return 0xc9d81859d60d6b21c688f4be93ca83e3be222728bb156ef5f4cf497f879f1e29;
            if (height == 8) return 0xd59b0c4544e0c60c8611eab0aaa402575f14ee784d22289c5d57f48c422a62d6;
            if (height == 9) return 0xccad21701f34f08bb8398a3dbc77e20e4c9c424930f3a8b31485bf059e2bdb20;
            return 0x8a42dfb49807647bfc49c906aef322aa0239d40e4cb675761e141bc7bfa530da;
        } else {
            if (height == 11) return 0x2adc0d948b2e3ecb642661590d2eec36d4e71e9acf382deb6574371800caf198;
            if (height == 12) return 0xf5845dfaed016de272342f346346a49d4b1694f622144d420558a38e46ac9dad;
            if (height == 13) return 0x3d7df854e6294bf433b64bbb8d0a82fa875a87b45b0016db27fc5752e54126ad;
            if (height == 14) return 0x72a991a101708716ff427c524404ab44f4d4d1f4e7e76c0ae8b967222164b348;
            if (height == 15) return 0x762c88fc52cf78a54401d247790f1bdb619d51d3458d1415c20d1422197cecc4;
            if (height == 16) return 0x8ede2209e94c8d5f8379d733dc8712b71a3888c1c4b70f3d6b22285f70bf4286;
            if (height == 17) return 0x425b18f07b3ac2f641977d2c294590565dd40b5d8414610568dca64628399975;
            if (height == 18) return 0x7e7d98718c0180e882e5963b9bd49810096912c273dfa38d8afdd6d39fde86ec;
            if (height == 19) return 0x8d35d491a29d846489e19688efff3c4cc7dbd54458058d49b30294074539f0b9;
            if (height == 20) return 0x824e385eea1953bcbc783bf900b18aa6fba129b6908765e986cf0968b491ec4f;
            revert TreeTooHigh();
        }
```

**File:** src/ratifiers/libraries/HashLib.sol (L58-63)
```text
        require(leafIndex >> proof.length == 0, LeafIndexOutOfRange());
        bytes32 currentHash = leafHash;
        for (uint256 i = 0; i < proof.length; i++) {
            currentHash = (leafIndex >> i) & 1 == 0 ? hashNode(currentHash, proof[i]) : hashNode(proof[i], currentHash);
        }
        return currentHash == root;
```
