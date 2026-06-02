Audit Report

## Title
Authorized Address Can De-Ratify Maker's Offer Tree via `setIsRootRatified` - (File: src/ratifiers/SetterRatifier.sol)

## Summary
`SetterRatifier.setIsRootRatified` applies an identical authorization guard for both ratification (`true`) and de-ratification (`false`). Any address that a maker has authorized via `midnight.setIsAuthorized` — for any purpose — can call `setIsRootRatified(maker, root, false)`, setting `isRootRatified[maker][root]` to `false` and causing all subsequent `take` calls against that maker's offer tree to revert with `NotRatified`. The attack is immediately repeatable for as long as the authorization remains active.

## Finding Description
The guard in `setIsRootRatified` is:

```solidity
require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
isRootRatified[maker][root] = newIsRootRatified;
``` [1](#0-0) 

This check is identical regardless of whether `newIsRootRatified` is `true` or `false`. The `isAuthorized` mapping is a flat, unscoped boolean with no operation-level granularity: [2](#0-1) 

`isRatified` enforces:

```solidity
require(isRootRatified[offer.maker][root], NotRatified());
``` [3](#0-2) 

**Exploit flow:**
1. `makerB` calls `midnight.setIsAuthorized(makerA, true, makerB)` for any legitimate reason (taker delegation, repay delegation, consumed management, callback contract).
2. `makerA` calls `setterRatifier.setIsRootRatified(makerB, root, false)`.
3. The guard passes because `isAuthorized[makerB][makerA] == true`.
4. `isRootRatified[makerB][root]` is set to `false`.
5. Every `midnight.take(offer, ...)` where `offer.maker == makerB` and `offer.ratifier == address(setterRatifier)` reverts with `NotRatified`.

The existing test suite explicitly validates that an authorized address can call `setIsRootRatified` on behalf of a maker in the ratification direction, confirming the code path is reachable: [4](#0-3) 

No existing test covers the de-ratification direction by an authorized (non-maker) address. [5](#0-4) 

## Impact Explanation
`makerB`'s entire offer tree — every root registered under `isRootRatified[makerB]` — can be set to `false` by any address `makerB` has ever authorized. All `take` calls against `makerB`'s offers using `SetterRatifier` revert with `NotRatified`, effectively freezing the offer tree. `makerB` can recover by re-ratifying, but the attack is immediately repeatable as long as the authorization remains active. Because Midnight authorization is coarse-grained and a single `setIsAuthorized` call covers all protocol operations, `makerB` cannot grant limited delegation without also exposing their ratifier state to this griefing vector. [6](#0-5) 

## Likelihood Explanation
The precondition — `makerB` having authorized `makerA` — is a normal, expected protocol operation used for taker delegation, repay delegation, consumed management, and callback contracts. Any of these legitimate use cases creates the precondition. The attack requires a single transaction from `makerA` and is immediately effective. `makerB` can recover by re-ratifying, but the attack is repeatable as long as the authorization remains active, making it a persistent griefing vector rather than a one-time disruption. [7](#0-6) 

## Recommendation
Restrict de-ratification (`newIsRootRatified == false`) to the maker only, requiring `maker == msg.sender`. Authorized addresses should only be permitted to ratify (`true`) on behalf of the maker, not to revoke ratification. The updated guard would be:

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

Alternatively, introduce a separate, scoped authorization mechanism for ratifier management distinct from the general `isAuthorized` delegation.

## Proof of Concept
```solidity
// 1. makerB authorizes makerA for taker delegation (legitimate use)
vm.prank(makerB);
midnight.setIsAuthorized(makerA, true, makerB);

// 2. makerB ratifies their offer tree root
vm.prank(makerB);
setterRatifier.setIsRootRatified(makerB, root, true);
assertTrue(setterRatifier.isRootRatified(makerB, root));

// 3. makerA de-ratifies makerB's root (attack)
vm.prank(makerA);
setterRatifier.setIsRootRatified(makerB, root, false); // passes auth check

assertFalse(setterRatifier.isRootRatified(makerB, root));

// 4. Any take against makerB's offer now reverts with NotRatified
vm.prank(address(midnight));
vm.expectRevert(ISetterRatifier.NotRatified.selector);
setterRatifier.isRatified(offer, abi.encode(root, 0, new bytes32[](0)));

// 5. Attack is immediately repeatable after makerB re-ratifies
vm.prank(makerB);
setterRatifier.setIsRootRatified(makerB, root, true);
vm.prank(makerA);
setterRatifier.setIsRootRatified(makerB, root, false); // repeats indefinitely
``` [8](#0-7)

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

**File:** src/interfaces/IMidnight.sol (L123-123)
```text
    function isAuthorized(address authorizer, address authorized) external view returns (bool);
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

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```

**File:** test/AuthorizationTest.sol (L274-288)
```text
    function testSetConsumedAuthorization(address user, address authorized) public {
        vm.assume(user != authorized);

        vm.prank(authorized);
        vm.expectRevert(IMidnight.Unauthorized.selector);
        midnight.setConsumed(bytes32(0), 100, user);

        vm.prank(user);
        midnight.setIsAuthorized(authorized, true, user);

        vm.prank(authorized);
        midnight.setConsumed(bytes32(0), 100, user);

        assertEq(midnight.consumed(user, bytes32(0)), 100);
    }
```
