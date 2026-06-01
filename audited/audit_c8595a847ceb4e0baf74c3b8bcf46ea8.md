### Title
`TreeTooHigh` Revert Escapes `isRatified` as Non-`RatifierFail` Error When Taker Supplies `proof.length = 21` - (`src/ratifiers/EcrecoverRatifier.sol`)

### Summary
`EcrecoverRatifier.isRatified` calls `HashLib.isLeaf` before calling `HashLib.offerTreeTypeHash(proof.length)`. Because `isLeaf` imposes no upper bound on `proof.length`, a taker can craft `ratifierData` with a 21-element proof and an attacker-controlled root that satisfies the Merkle check, causing execution to reach `offerTreeTypeHash(21)` which unconditionally reverts with `TreeTooHigh`. This revert propagates raw through `take()`, bypassing the `RatifierFail` wrapper that `Midnight` uses to normalise ratifier failures.

### Finding Description

**Exact code path:**

In `EcrecoverRatifier.isRatified` (lines 37–39):
```solidity
require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof()); // line 37
require(!isRootCanceled[offer.maker][root], RootCanceled());                                // line 38
bytes32 structHash = keccak256(abi.encode(HashLib.offerTreeTypeHash(proof.length), root)); // line 39
```

`HashLib.isLeaf` (lines 53–64) only checks `leafIndex >> proof.length == 0`; it does **not** cap `proof.length` at 20. With `leafIndex = 0`, the shift is `0 >> 21 = 0`, so the guard passes. The loop then runs 21 times over the attacker-supplied `proof` array, producing a deterministic `currentHash`. The attacker sets `root = currentHash` in `ratifierData`, making `isLeaf` return `true`.

Execution then reaches line 39. `HashLib.offerTreeTypeHash(21)` enters the `else` branch (lines 35–47 of `HashLib.sol`), finds no matching `if`, and executes `revert TreeTooHigh()`.

This revert propagates back through `isRatified` and into `Midnight.take` at line 356:
```solidity
require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```
The `require` only emits `RatifierFail` when `isRatified` **returns** a wrong value. When `isRatified` **reverts**, the revert bubbles through `take()` unchanged as `TreeTooHigh`, not `RatifierFail`.

**Attacker-controlled inputs:**
- `ratifierData`: fully taker-controlled; decoded into `(sig, root, leafIndex, proof)` at line 35–36 of `EcrecoverRatifier.sol`.
- Attacker sets `proof = new bytes32[](21)` (any 21 values), `leafIndex = 0`, and `root` = the Merkle root computed from those 21 siblings and the offer's leaf hash.

**Why existing checks fail:**
- `isLeaf` has no `proof.length <= 20` guard.
- The `isRootCanceled` check (line 38) passes because the attacker's crafted root has never been canceled.
- The signature check (lines 42–44) is never reached.
- The `require(..., RatifierFail())` wrapper in `take()` only fires on a return-value mismatch, not on a revert.

### Impact Explanation
Any call to `take()` using `EcrecoverRatifier` as the ratifier reverts with the internal `HashLib.TreeTooHigh` error selector instead of the protocol-standard `IMidnight.RatifierFail` selector. Callers (including smart-contract integrators, aggregators, or multicall sequences) that catch `RatifierFail` to handle graceful fallback will not catch `TreeTooHigh`, causing unexpected revert propagation. The internal error selector is exposed to the taker, leaking implementation details. No persistent state change occurs, so the maker's offer is not directly corrupted, but the invariant that all ratifier failures surface as `RatifierFail` is broken.

### Likelihood Explanation
Preconditions: any valid offer using `EcrecoverRatifier` as its ratifier (the standard ratifier). The taker needs only to craft `ratifierData` with a 21-element proof and a matching root — no privileged access, no special state, no tokens required. The computation is pure off-chain arithmetic. Repeatable on every block, against any offer using this ratifier.

### Recommendation
Add an explicit length guard in `HashLib.isLeaf` or at the top of `EcrecoverRatifier.isRatified` before the `isLeaf` call:

```solidity
require(proof.length <= 20, TreeTooHigh()); // or InvalidProof()
```

This ensures that any `proof.length > 20` is rejected with a clean, expected error before reaching `offerTreeTypeHash`, restoring the invariant that invalid ratifier data always produces a normalised failure.

### Proof of Concept

```solidity
function testTreeTooHighEscapesRatifierFail() public {
    // Setup: valid offer with EcrecoverRatifier as ratifier
    // (maker has authorized the ratifier as in integration tests)

    // Craft proof of length 21 with leafIndex = 0
    bytes32[] memory proof = new bytes32[](21);
    // proof elements can be anything; compute root to satisfy isLeaf
    bytes32 leafHash = HashLib.hashOffer(lenderOffer);
    bytes32 currentHash = leafHash;
    for (uint256 i = 0; i < 21; i++) {
        proof[i] = bytes32(uint256(i + 1)); // arbitrary
        currentHash = keccak256(abi.encodePacked(currentHash, proof[i]));
    }
    bytes32 craftedRoot = currentHash;

    Signature memory sig = Signature({v: 0, r: 0, s: 0}); // irrelevant, never reached
    bytes memory ratifierData = abi.encode(sig, craftedRoot, uint256(0), proof);

    // Assert revert is TreeTooHigh, NOT RatifierFail
    vm.expectRevert(HashLib.TreeTooHigh.selector);
    // NOT: vm.expectRevert(IMidnight.RatifierFail.selector);
    vm.prank(borrower);
    midnight.take(lenderOffer, ratifierData, 0, borrower, borrower, address(0), hex"");
}
```

**Expected assertion:** revert selector is `HashLib.TreeTooHigh` (not `IMidnight.RatifierFail`), confirming the invariant violation. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** src/ratifiers/EcrecoverRatifier.sol (L37-39)
```text
        require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
        require(!isRootCanceled[offer.maker][root], RootCanceled());
        bytes32 structHash = keccak256(abi.encode(HashLib.offerTreeTypeHash(proof.length), root));
```

**File:** src/ratifiers/libraries/HashLib.sol (L44-47)
```text
            if (height == 19) return 0x8d35d491a29d846489e19688efff3c4cc7dbd54458058d49b30294074539f0b9;
            if (height == 20) return 0x824e385eea1953bcbc783bf900b18aa6fba129b6908765e986cf0968b491ec4f;
            revert TreeTooHigh();
        }
```

**File:** src/ratifiers/libraries/HashLib.sol (L53-58)
```text
    function isLeaf(bytes32 root, bytes32 leafHash, uint256 leafIndex, bytes32[] memory proof)
        internal
        pure
        returns (bool)
    {
        require(leafIndex >> proof.length == 0, LeafIndexOutOfRange());
```

**File:** src/Midnight.sol (L355-356)
```text
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```
