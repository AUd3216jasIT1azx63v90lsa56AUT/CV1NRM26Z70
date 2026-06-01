### Title
Transitive Authorization Allows Unprivileged Address to Ratify Arbitrary Roots for Maker - (File: src/ratifiers/SetterRatifier.sol)

### Summary
`SetterRatifier.setIsRootRatified` gates access via `IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender)`, a flat one-hop check. Because `Midnight.setIsAuthorized` itself also accepts any already-authorized address as the caller on behalf of the authorizer, authorization is fully transitive: an authorized operator can grant the same privilege to any third party without the maker's knowledge. An attacker who convinces or controls any one of the maker's authorized operators can therefore have themselves added to `isAuthorized[maker]` and then call `setIsRootRatified(maker, attacker_root, true)`, ratifying a Merkle root the maker never approved.

### Finding Description

**Code path – `setIsRootRatified`:** [1](#0-0) 

The only guard is `maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender)`. It performs a single flat lookup into `isAuthorized[maker][msg.sender]`.

**Code path – `setIsAuthorized`:** [2](#0-1) 

The guard is `onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender]`. Any address already in `isAuthorized[maker]` satisfies this check and can therefore write any new entry into `isAuthorized[maker][*]`.

**Confirmed by existing test:** [3](#0-2) 

The test `testSetIsAuthorizedAuthorization` explicitly asserts that an authorized address can authorize a third party on behalf of the original user. This is not a hypothetical — it is tested, working behavior.

**Exploit flow:**

1. `maker` calls `midnight.setIsAuthorized(operatorA, true, maker)` → `isAuthorized[maker][operatorA] = true` (legitimate).
2. `operatorA` calls `midnight.setIsAuthorized(attackerB, true, maker)` → passes because `isAuthorized[maker][operatorA]` is true; sets `isAuthorized[maker][attackerB] = true` without maker's knowledge.
3. `attackerB` calls `setterRatifier.setIsRootRatified(maker, attacker_root, true)` → passes because `isAuthorized(maker, attackerB)` is now true.
4. `isRootRatified[maker][attacker_root] = true`.

**Why the existing check fails:**
`setIsRootRatified` only checks the flat `isAuthorized` mapping. It cannot distinguish between an address the maker directly authorized and one that was inserted by a transitive hop. There is no depth limit, no maker-signature requirement, and no separate registry for ratification-specific permissions.

### Impact Explanation
Once `isRootRatified[maker][attacker_root]` is `true`, `isRatified` will accept any offer whose hash is a leaf

### Citations

**File:** src/ratifiers/SetterRatifier.sol (L24-28)
```text
    function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootRatified[maker][root] = newIsRootRatified;
        emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
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

**File:** test/AuthorizationTest.sol (L290-304)
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
    }
```
