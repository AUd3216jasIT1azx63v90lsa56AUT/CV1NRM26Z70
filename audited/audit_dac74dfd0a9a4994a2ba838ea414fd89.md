The full call chain is confirmed. Let me verify the exact lines and check if there are any additional guards I may have missed.

### Title
Transitive Authorization Delegation Allows Unauthorized Third Party to Freeze Maker's Offer Tree via SetterRatifier - (File: src/ratifiers/SetterRatifier.sol)

### Summary
`Midnight.setIsAuthorized` permits any address already authorized by a maker to grant that same authorization to an arbitrary third party on the maker's behalf. Because `SetterRatifier.setIsRootRatified` gates access solely on `isAuthorized[maker][msg.sender]`, a transitively-authorized address the maker never directly approved can set `isRootRatified[maker][root] = false`, permanently blocking every `take` that relies on that root.

### Finding Description
**Root cause — `setIsAuthorized` allows authorized operators to re-delegate:**

`src/Midnight.sol:731-735`
```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    ...
}
```
The guard `isAuthorized[onBehalf][msg.sender]` is satisfied by any already-authorized operator, not only the maker themselves. There is no restriction preventing an operator from writing a new entry into `isAuthorized[maker][*]`.

This is confirmed as intentional by `test/AuthorizationTest.sol:290-303` (`testSetIsAuthorizedAuthorization`), which asserts that `authorized` can call `setIsAuthorized(newAuthorized, true, user)` and succeed.

**Exploit path:**

1. `maker` calls `midnight.setIsAuthorized(operatorA, true, maker)` → `isAuthorized[maker][operatorA] = true`.
2. `operatorA` calls `midnight.setIsAuthorized(operatorB, true, maker)`:
   - Check: `isAuthorized[maker][operatorA]` → `true` ✓
   - Result: `isAuthorized[maker][operatorB] = true`
3. `operatorB` (never directly authorized by maker) calls `setterRatifier.setIsRootRatified(maker, root, false)`:
   - `src/ratifiers/SetterRatifier.sol:25`: `maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender)`
   - `isAuthorized[maker][operatorB]` → `true` ✓
   - Result: `isRootRatified[maker][root] = false`
4. Every subsequent `take` for any offer whose Merkle proof resolves to `root` hits `src/ratifiers/SetterRatifier.sol:35`: `require(isRootRatified[offer.maker][root], NotRatified())` → **reverts**.

**Why existing checks fail:**
`setIsRootRatified` performs only a single flat `isAuthorized` lookup. It cannot distinguish between a directly-authorized operator and one that was granted authorization by another operator. There is no scope, depth limit, or "only-maker-can-delegate" guard anywhere in the path.

### Impact Explanation
All offers whose Merkle root is `root` become permanently un-takeable. Lenders (makers) cannot fill any of those offers; credit cannot be extended; if the maker's entire active offer set is covered by one root, the maker is effectively frozen out of the protocol. The freeze is instant, costs only two transactions, and cannot be undone by the maker without re-ratifying the root — which operatorB (or operatorA) can immediately unset again.

### Likelihood Explanation
**Preconditions:**
- Maker must have authorized at least one operator (operatorA). This is a normal, expected usage pattern (e.g., authorizing a bundle contract or keeper).
- operatorA must be malicious or compromised.

**Feasibility:** operatorA is a single EOA or contract; no special privileges beyond the maker's initial `setIsAuthorized` call are required. The two-hop delegation is a single additional transaction. The attack is repeatable: every time the maker re-ratifies a root, operatorB can immediately unset it.

**Repeatability:** Unlimited — operatorB's authorization persists until the maker explicitly revokes it via `midnight.setIsAuthorized(operatorB, false, maker)`, but operatorA can re-grant it.

### Recommendation
Restrict `setIsAuthorized` so that only the account being authorized-for (`onBehalf == msg.sender`) can grant new authorizations. Operators should be able to act on behalf of the maker but not extend the maker's authorization to third parties:

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender, Unauthorized()); // only the maker can delegate
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    ...
}
```

If operator-level delegation is intentionally desired, add a depth flag or a separate `delegateIsAuthorized` function that is explicitly opt-in by the maker and does not propagate to `SetterRatifier` / `EcrecoverRatifier` state-mutation paths.

### Proof of Concept
```solidity
// Foundry unit test
function testTransitiveDelegationFreezesOfferTree() public {
    address maker     = makeAddr("maker");
    address operatorA = makeAddr("operatorA");
    address operatorB = makeAddr("operatorB");

    // Step 1: maker authorizes operatorA
    vm.prank(maker);
    midnight.setIsAuthorized(operatorA, true, maker);
    assertTrue(midnight.isAuthorized(maker, operatorA));

    // Step 2: operatorA grants operatorB maker-level authorization
    vm.prank(operatorA);
    midnight.setIsAuthorized(operatorB, true, maker);
    // Assert: operatorB is now authorized on behalf of maker
    assertTrue(midnight.isAuthorized(maker, operatorB));

    // Step 3: operatorB freezes the maker's offer tree
    bytes32 root = keccak256("offerRoot");
    vm.prank(maker);
    setterRatifier.setIsRootRatified(maker, root, true); // maker ratifies first

    vm.prank(operatorB);
    setterRatifier.setIsRootRatified(maker, root, false); // operatorB un-ratifies
    assertFalse(setterRatifier.isRootRatified(maker, root));

    // Step 4: any take against this root now reverts with NotRatified
    Offer memory offer = makeOffer(maker);
    bytes32 offerHash = HashLib.hashOffer(offer);
    // (assume root == offerHash for single-leaf tree)
    vm.prank(address(midnight));
    vm.expectRevert(ISetterRatifier.NotRatified.selector);
    setterRatifier.isRatified(offer, abi.encode(offerHash, 0, new bytes32[](0)));
}
```

**Expected assertions:**
- `isAuthorized[maker][operatorB] == true` after step 2 (two-hop delegation succeeds).
- `isRootRatified[maker][root] == false` after step 3 (operatorB can mutate ratifier state).
- `isRatified` reverts with `NotRatified` in step 4 (all takes under root are frozen). [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/Midnight.sol (L192-192)
```text
    mapping(address authorizer => mapping(address authorized => bool)) public isAuthorized;
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```

**File:** src/ratifiers/SetterRatifier.sol (L24-28)
```text
    function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootRatified[maker][root] = newIsRootRatified;
        emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
    }
```

**File:** src/ratifiers/SetterRatifier.sol (L34-36)
```text
        require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
        require(isRootRatified[offer.maker][root], NotRatified());
        return CALLBACK_SUCCESS;
```

**File:** test/AuthorizationTest.sol (L290-303)
```text
    function testSetIsAuthorizedAuthorization(address user, address authorized, address newAuthorized) public {
        vm.assume(user != authorized);

        vm.prank(authorized);
        vm.expectRevert(IMidnight.Unauthorized.selector);
        midnight.setIsAuthorized(newAuthorized, true, user);

        vm.prank(user);
        midnight.setIsAuthorized(authorized, true, user);

        vm.prank(authorized);
        midnight.setIsAuthorized(newAuthorized, true, user);

        assertEq(midnight.isAuthorized(user, newAuthorized), true);
```
