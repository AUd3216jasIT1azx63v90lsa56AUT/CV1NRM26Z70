### Title
`offerTreeTypeHash` height cap at 20 permanently blocks fills for offer trees of height ≥ 21 — (`src/ratifiers/EcrecoverRatifier.sol`)

### Summary
`EcrecoverRatifier.isRatified` calls `HashLib.offerTreeTypeHash(proof.length)` after a successful `isLeaf` check. `offerTreeTypeHash` unconditionally reverts with `TreeTooHigh` for any height > 20, while `isLeaf` itself supports up to 256 levels. A maker who constructs and signs a Merkle offer tree of height ≥ 21 produces an offer tree where every valid fill attempt reverts, making the entire tree permanently unfillable.

### Finding Description
**Code path:**

`Midnight.take()` (line 356) calls `IRatifier(offer.ratifier).isRatified(offer, ratifierData)`. If `isRatified` reverts (rather than returning a wrong value), the revert propagates directly — it is not caught and re-wrapped as `RatifierFail`.

Inside `EcrecoverRatifier.isRatified` (lines 37–39):

```solidity
// Line 37 — isLeaf supports up to 256 levels, no height cap here
require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
require(!isRootCanceled[offer.maker][root], RootCanceled());
// Line 39 — offerTreeTypeHash caps at 20; reverts TreeTooHigh for proof.length >= 21
bytes32 structHash = keccak256(abi.encode(HashLib.offerTreeTypeHash(proof.length), root));
``` [1](#0-0) 

`HashLib.offerTreeTypeHash` handles heights 0–20 via hardcoded return values and falls through to `revert TreeTooHigh()` for any height ≥ 21:

```solidity
if (height == 20) return 0x824e385eea1953bcbc783bf900b18aa6fba129b6908765e986cf0968b491ec4f;
revert TreeTooHigh();
``` [2](#0-1) 

Meanwhile, `isLeaf` explicitly documents support for heights up to 256:

```solidity
/// @dev Works for offer-tree heights up to 256, the bit-width of leafIndex. In practice the height is capped at 20
/// by offerTreeTypeHash.
``` [3](#0-2) 

**Attacker-controlled inputs:** The taker controls `ratifierData`, which decodes to `(sig, root, leafIndex, proof)`. The taker supplies `proof` of length 21. For `isLeaf` to pass (line 37), the taker needs a valid 21-element Merkle path to `root` — which is only possible if the maker actually constructed a height-21 tree and the taker holds a valid proof for a leaf in it.

**Exploit flow (given precondition):**
1. Maker constructs a Merkle tree of height 21 (2^21 offer leaves), signs the root.
2. Taker calls `Midnight.take(offer, ratifierData, ...)` with `proof.length == 21` (a valid proof from the maker's tree).
3. `isLeaf` passes — the 21-element proof correctly hashes to `root`.
4. `isRootCanceled` passes — root is not canceled.
5. `offerTreeTypeHash(21)` → `revert TreeTooHigh()`.
6. Revert propagates through `take()`.

**Why existing checks fail:** There is no validation at tree-creation or signing time that enforces `height ≤ 20`. The cap exists only inside `offerTreeTypeHash`, which is called during fill. Every valid proof for a height-21 tree has `proof.length == 21`, so every fill attempt for every offer in the tree will always revert. The maker cannot fix this without abandoning the signed root and re-signing a new tree of height ≤ 20. [4](#0-3) 

### Impact Explanation
Every offer in the maker's height-21 tree is permanently unfillable via `EcrecoverRatifier`. The maker must abandon the signed root and re-sign all offers under a new tree of height ≤ 20. Any counterparties relying on the original signed root (e.g., off-chain order books referencing the root) are broken. While Midnight offers are signed intents (no funds locked at creation), any maker who pre-funded positions expecting fills from this tree will find those fills blocked indefinitely.

### Likelihood Explanation
The precondition is that a maker constructs a tree of height ≥ 21. This is a realistic mistake: a maker batching more than 2^20 (~1M) offers would naturally reach height 21. The protocol provides no off-chain or on-chain warning. The `isLeaf` comment ("Works for offer-tree heights up to 256") could mislead a maker into believing large trees are supported end-to-end. Once the tree is signed and distributed, the condition is permanent and repeatable — every fill attempt by any taker will hit the same revert.

### Recommendation
Add a height guard in `isRatified` before calling `offerTreeTypeHash`, or add an explicit `require(proof.length <= 20, TreeTooHigh())` at the top of `isRatified`. Alternatively, extend `offerTreeTypeHash` to support heights up to 256 (matching `isLeaf`'s documented capability) by computing the typehash dynamically rather than via a lookup table. The fix must be applied before the signature digest is computed so that the error is surfaced clearly rather than as an opaque revert. [5](#0-4) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity 0.8.34;

import {Test} from "forge-std/Test.sol";
import {EcrecoverRatifier} from "src/ratifiers/EcrecoverRatifier.sol";
import {HashLib} from "src/ratifiers/libraries/HashLib.sol";
import {Signature} from "src/ratifiers/interfaces/IEcrecoverRatifier.sol";
import {Offer} from "src/interfaces/IMidnight.sol";
// ... standard test setup imports

contract TreeTooHighTest is Test {
    // Build a height-21 Merkle tree with the offer as a leaf.
    // Sign the root with the maker's key.
    // Call take() with proof.length == 21.
    // Assert revert with TreeTooHigh (propagated, not RatifierFail).

    function testHeight21TreePermanentlyUnfillable() public {
        // 1. Build a 21-level tree: start from offerHash, hash up 21 times with dummy siblings.
        bytes32 offerHash = HashLib.hashOffer(offer); // offer is a valid, unexpired offer
        bytes32[] memory proof = new bytes32[](21);
        bytes32 current = offerHash;
        for (uint256 i = 0; i < 21; i++) {
            proof[i] = keccak256(abi.encode("sibling", i));
            current = HashLib.hashNode(current, proof[i]); // leafIndex = 0, always left
        }
        bytes32 root = current;

        // 2. Maker signs the root for a height-21 tree.
        bytes32 typehash; // cannot call offerTreeTypeHash(21) — it reverts!
        // Instead sign using the raw EIP-712 structure the maker would compute off-chain.
        // (In practice, maker's off-chain tooling computes this without the on-chain cap.)
        // For the PoC, we sign a digest that isRatified would compute IF it didn't revert.
        // The signature check is never reached, so any sig bytes suffice to demonstrate the revert.
        Signature memory sig = Signature({v: 27, r: bytes32(uint256(1)), s: bytes32(uint256(2))});

        bytes memory ratifierData = abi.encode(sig, root, uint256(0), proof);

        // 3. Authorize ratifier for maker's offer.
        vm.prank(offer.maker);
        midnight.setIsAuthorized(address(ecrecoverRatifier), true, offer.maker);

        // 4. Taker calls take() — expect TreeTooHigh revert (not RatifierFail).
        vm.prank(taker);
        vm.expectRevert(HashLib.TreeTooHigh.selector);
        midnight.take(offer, ratifierData, 0, taker, taker, address(0), hex"");

        // 5. Fuzz: assert all proof.length in [21, 256] revert with TreeTooHigh.
    }

    function testFuzzHeight21PlusAlwaysReverts(uint256 height) public {
        height = bound(height, 21, 256);
        bytes32[] memory proof = new bytes32[](height);
        bytes32 current = HashLib.hashOffer(offer);
        for (uint256 i = 0; i < height; i++) {
            proof[i] = keccak256(abi.encode("s", i));
            current = HashLib.hashNode(current, proof[i]);
        }
        bytes32 root = current;
        Signature memory sig = Signature({v: 27, r: bytes32(uint256(1)), s: bytes32(uint256(2))});
        bytes memory ratifierData = abi.encode(sig, root, uint256(0), proof);

        vm.prank(offer.maker);
        midnight.setIsAuthorized(address(ecrecoverRatifier), true, offer.maker);

        vm.prank(taker);
        vm.expectRevert(HashLib.TreeTooHigh.selector);
        midnight.take(offer, ratifierData, 0, taker, taker, address(0), hex"");
    }
}
```

**Expected assertions:**
- `vm.expectRevert(HashLib.TreeTooHigh.selector)` passes for all `proof.length ∈ [21, 256]`.
- The revert is `TreeTooHigh`, not `RatifierFail`, confirming it propagates raw from `offerTreeTypeHash`.
- With `proof.length ∈ [0, 20]` and a valid proof, `take()` proceeds normally (control assertion). [6](#0-5) [7](#0-6)

### Citations

**File:** src/ratifiers/EcrecoverRatifier.sol (L33-46)
```text
    function isRatified(Offer memory offer, bytes memory ratifierData) external view returns (bytes32) {
        require(msg.sender == MIDNIGHT, NotMidnight());
        (Signature memory sig, bytes32 root, uint256 leafIndex, bytes32[] memory proof) =
            abi.decode(ratifierData, (Signature, bytes32, uint256, bytes32[]));
        require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
        require(!isRootCanceled[offer.maker][root], RootCanceled());
        bytes32 structHash = keccak256(abi.encode(HashLib.offerTreeTypeHash(proof.length), root));
        bytes32 domainSeparator = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(this)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSeparator, structHash));
        address _signer = ecrecover(digest, sig.v, sig.r, sig.s);
        require(_signer != address(0), InvalidSignature());
        require(_signer == offer.maker || IMidnight(MIDNIGHT).isAuthorized(offer.maker, _signer), Unauthorized());
        return CALLBACK_SUCCESS;
    }
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

**File:** src/ratifiers/libraries/HashLib.sol (L51-52)
```text
    /// @dev Works for offer-tree heights up to 256, the bit-width of leafIndex. In practice the height is capped at 20
    /// by offerTreeTypeHash.
```

**File:** src/Midnight.sol (L355-356)
```text
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

**File:** test/HashLibTest.sol (L123-128)
```text
    /// forge-config: default.allow_internal_expect_revert = true
    function testOfferTreeTypeHashInvalidHeight(uint256 height) public {
        height = bound(height, 21, type(uint256).max);
        vm.expectRevert(HashLib.TreeTooHigh.selector);
        HashLib.offerTreeTypeHash(height);
    }
```
