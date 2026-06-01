### Title
Authorized Address Can De-Ratify Maker's Offer Tree via `setIsRootRatified` - (File: src/ratifiers/SetterRatifier.sol)

### Summary
`SetterRatifier.setIsRootRatified` uses the same coarse-grained Midnight authorization check for both ratification (`true`) and de-ratification (`false`). Any address authorized by a maker via `midnight.setIsAuthorized` — regardless of the original purpose of that authorization — can call `setIsRootRatified(maker, root, false)` and freeze all `take` calls against that maker's entire offer tree.

### Finding Description
The authorization guard in `SetterRatifier.setIsRootRatified` is:

```solidity
require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
``` [1](#0-0) 

`IMidnight.isAuthorized` is a flat, unscoped boolean mapping:

```solidity
mapping(address authorizer => mapping(address authorized => bool)) public isAuthorized;
``` [2](#0-1) 

There is no distinction between setting `newIsRootRatified = true` vs `false` in the guard. Both operations pass the same check. The `isRatified` callback enforces:

```solidity
require(isRootRatified[offer.maker][root], NotRatified());
``` [3](#0-2) 

**Exploit flow:**

1. makerB calls `midnight.setIsAuthorized(makerA, true, makerB)` for any reason (e.g., to allow makerA to take on behalf, repay, or manage consumed nonces).
2. makerA calls `setterRatifier.setIsRootRatified(makerB, root, false)`.
3. The guard passes because `isAuthorized[makerB][makerA] == true`.
4. `isRootRatified[makerB][root]` is set to `false`.
5. Every subsequent `midnight.take(offer, ...)` where `offer.maker == makerB` and `offer.ratifier == address(setterRatifier)` reverts with `NotRatified`.

The existing test suite explicitly validates that an authorized address can call `setIsRootRatified` on behalf of a maker (ratification direction), confirming the path is reachable: [4](#0-3) 

No existing check distinguishes between the `true` and `false` cases, and no test covers the de-ratification direction by an authorized address.

### Impact Explanation
makerB's entire offer tree — every root registered under `isRootRatified[makerB]` — can be set to `false` by any address makerB has ever authorized. All `take` calls against makerB's offers revert with `NotRatified`, effectively freezing the offer tree for as long as makerA retains authorization or until makerB re-ratifies. Because Midnight authorization is coarse-grained and a single `setIsAuthorized` call covers all protocol operations, makerB cannot grant limited delegation without also exposing their ratifier state to this attack. [5](#0-4) 

### Likelihood Explanation
The precondition — makerB having authorized makerA — is a normal, expected protocol operation. Authorization is used for taker delegation, repay delegation, consumed management, and callback contracts. Any of these legitimate use cases creates the precondition. The attack requires a single transaction from makerA and is immediately effective. makerB can recover by re-ratifying, but the attack is repeatable as long as the authorization remains active. [6](#0-5) 

### Recommendation
Restrict de-ratification (`newIsRootRatified == false`) to the maker only, while allowing authorized addresses to ratify on behalf:

```solidity
function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
    if (newIsRootRatified) {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
    } else {
        require(maker == msg.sender, Unauthorized());
    }
    isRootRatified[maker][root] = newIsRootRatified;
    emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
}
```

This preserves the intended delegation for ratification while ensuring only the maker can revoke their own offer tree. [7](#0-6) 

### Proof of Concept

```solidity
function testAuthorizedCanDeRatifyMakerRoot() public {
    address makerB = lender;
    address makerA = borrower;
    bytes32 root = keccak256("root");

    // makerB ratifies a root
    vm.prank(makerB);
    setterRatifier.setIsRootRatified(makerB, root, true);
    assertTrue(setterRatifier.isRootRatified(makerB, root));

    // makerB authorizes makerA for unrelated purposes
    vm.prank(makerB);
    midnight.setIsAuthorized(makerA, true, makerB);

    // makerA de-ratifies makerB's root — should revert but does not
    vm.prank(makerA);
    setterRatifier.setIsRootRatified(makerB, root, false);

    // Assert: makerB's root is now de-ratified
    assertFalse(setterRatifier.isRootRatified(makerB, root));

    // Assert: take for makerB's offer reverts NotRatified
    Offer memory offer = makeOffer(makerB);
    vm.prank(address(midnight));
    vm.expectRevert(ISetterRatifier.NotRatified.selector);
    setterRatifier.isRatified(offer, abi.encode(root, 0, new bytes32[](0)));
}
```

Expected: the `setIsRootRatified(makerB, root, false)` call succeeds (no revert), and the subsequent `isRatified` call reverts with `NotRatified`, confirming the offer tree is frozen. [8](#0-7)

### Citations

**File:** src/ratifiers/SetterRatifier.sol (L24-28)
```text
    function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootRatified[maker][root] = newIsRootRatified;
        emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
    }
```

**File:** src/ratifiers/SetterRatifier.sol (L35-35)
```text
        require(isRootRatified[offer.maker][root], NotRatified());
```

**File:** src/Midnight.sol (L192-192)
```text
    mapping(address authorizer => mapping(address authorized => bool)) public isAuthorized;
```

**File:** src/Midnight.sol (L731-734)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
```

**File:** test/SetterRatifierTest.sol (L48-61)
```text
    function testIsRatifiedAuthorizedSetterCanRatifyOnBehalf() public {
        Offer memory offer = makeOffer(lender);
        bytes32 _root = HashLib.hashOffer(offer);

        vm.prank(lender);
        midnight.setIsAuthorized(borrower, true, lender);

        vm.prank(borrower);
        setterRatifier.setIsRootRatified(lender, _root, true);

        vm.prank(address(midnight));
        bytes32 result = setterRatifier.isRatified(offer, abi.encode(_root, 0, new bytes32[](0)));
        assertEq(result, CALLBACK_SUCCESS);
    }
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
