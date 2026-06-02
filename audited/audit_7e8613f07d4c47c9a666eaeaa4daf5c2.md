Audit Report

## Title
Authorized Operator Can Create Persistent Sub-Authorizations That Survive Revocation - (File: src/Midnight.sol)

## Summary
`setIsAuthorized` permits any currently-authorized address to grant new authorizations on behalf of a user with no restriction on sub-delegation depth or scope. When a victim later revokes the original operator, any secondary addresses that operator added remain authorized indefinitely. The victim has no enumeration mechanism to discover or bulk-revoke these hidden sub-authorizations.

## Finding Description
The access check in `setIsAuthorized` at [1](#0-0)  is:

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
```

The guard only verifies that `msg.sender` is currently authorized for `onBehalf`; it places no restriction on what the authorized caller may do next. The exploit path:

1. **Precondition**: victim calls `setIsAuthorized(attacker, true, victim)` → `isAuthorized[victim][attacker] = true`.
2. **Attacker call**: attacker calls `setIsAuthorized(attacker2, true, victim)`. The check `isAuthorized[victim][attacker]` is `true`, so it passes. Result: `isAuthorized[victim][attacker2] = true`.
3. **Revocation**: victim calls `setIsAuthorized(attacker, false, victim)` → `isAuthorized[victim][attacker] = false`.
4. **Bad state**: `isAuthorized[victim][attacker2]` remains `true`. `attacker2` retains full operator-level access indefinitely.

The existing test `testSetIsAuthorizedAuthorization` at [2](#0-1)  confirms step 2 is reachable and succeeds (lines 300–303), but never tests the revocation scenario.

The Certora rule `onlyAuthorizedCanChangeIsAuthorized` at [3](#0-2)  only asserts that the caller was authorized at call time (`authorizerIsAuthorized` is evaluated before `f(e, args)`); it does not assert anything about the state after a subsequent revocation, so it does not catch this scenario.

## Impact Explanation
Any address the victim ever authorizes can silently plant one or more additional authorized addresses. After the victim revokes the original operator, the planted addresses retain full operator-level access: they can withdraw victim's credit units, withdraw victim's collateral, cancel victim's offers via `setConsumed`, and recursively add yet more authorized addresses. The victim has no enumeration API to discover these hidden entries and no atomic "revoke all" primitive. This constitutes unauthorized movement of assets and unauthorized state changes — both in-scope impact classes per RESEARCHER.md.

## Likelihood Explanation
The precondition is that the victim must have authorized the attacker at some point (e.g., a DeFi aggregator, a bundle executor, or a compromised key). This is a normal, expected usage pattern. The attacker needs only one transaction while authorized to plant `attacker2`. The attack is repeatable and the planted authorization is permanent until the victim explicitly revokes `attacker2` by address — which they cannot do if they are unaware of it.

## Recommendation
Restrict `setIsAuthorized` so that only `onBehalf == msg.sender` (i.e., the account itself) can grant new authorizations. Authorized operators should only be permitted to revoke their own authorization (i.e., `authorized == msg.sender && newIsAuthorized == false`), not to grant new ones. Alternatively, introduce a delegation depth limit (e.g., only direct self-grants are allowed) or require that sub-delegations are explicitly scoped and expire with the parent authorization.

## Proof of Concept
Add the following test to `test/AuthorizationTest.sol`:

```solidity
function testSubAuthorizationSurvivesRevocation(
    address user, address attacker, address attacker2
) public {
    vm.assume(user != attacker && user != attacker2 && attacker != attacker2);

    // Step 1: victim authorizes attacker
    vm.prank(user);
    midnight.setIsAuthorized(attacker, true, user);

    // Step 2: attacker plants attacker2
    vm.prank(attacker);
    midnight.setIsAuthorized(attacker2, true, user);
    assertEq(midnight.isAuthorized(user, attacker2), true);

    // Step 3: victim revokes attacker
    vm.prank(user);
    midnight.setIsAuthorized(attacker, false, user);
    assertEq(midnight.isAuthorized(user, attacker), false);

    // Step 4: attacker2 still authorized — BUG
    assertEq(midnight.isAuthorized(user, attacker2), true);
}
```

This test will pass against the current implementation, demonstrating the persistent sub-authorization survives revocation.

### Citations

**File:** src/Midnight.sol (L731-733)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
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

**File:** certora/specs/OnlyAuthorizedCanChange.spec (L102-110)
```text
rule onlyAuthorizedCanChangeIsAuthorized(env e, method f, calldataarg args, address authorizer, address authorized) filtered { f -> !f.isView } {
    bool authorizerIsAuthorized = authorizer == e.msg.sender || isAuthorized(authorizer, e.msg.sender);

    bool isAuthorizedBefore = isAuthorized(authorizer, authorized);
    f(e, args);
    bool isAuthorizedAfter = isAuthorized(authorizer, authorized);

    assert isAuthorizedAfter == isAuthorizedBefore || authorizerIsAuthorized;
}
```
