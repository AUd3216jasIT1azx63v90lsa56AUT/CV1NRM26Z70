### Title
Authorized Delegate Can Grant Irrevocable Sub-Authorizations via EcrecoverAuthorizer — (`src/periphery/EcrecoverAuthorizer.sol`)

### Summary

`EcrecoverAuthorizer.setIsAuthorized` accepts signatures from any address that is currently `isAuthorized` for the stated `authorizer`, not only from the authorizer themselves. This allows an authorized delegate (attacker) to sign and submit `Authorization` structs that grant further addresses (`attacker2`) authorization over the original authorizer's (`A`) position. When `A` later revokes the attacker, `attacker2`'s authorization persists with no mechanism to remove it, violating the invariant that revoking a delegate must not leave residual authorizations granted by that delegate.

### Finding Description

**Exact code path:**

`EcrecoverAuthorizer.setIsAuthorized` (lines 24–48) performs the following signer check:

```solidity
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
``` [1](#0-0) 

If the recovered signer is any address for which `isAuthorized[authorizer][signer] == true`, the check passes. On success, it calls:

```solidity
IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
``` [2](#0-1) 

`Midnight.setIsAuthorized` then writes `isAuthorized[A][attacker2] = true` unconditionally. [3](#0-2) 

**Exploit flow:**

1. A calls `midnight.setIsAuthorized(attacker, true, A)` → `isAuthorized[A][attacker] = true` (legitimate grant).
2. Attacker constructs `Authorization{authorizer: A, authorized: attacker2, isAuthorized: true, nonce: nonce[A], deadline: T+1}` and signs it with **attacker's own private key** (not A's).
3. Attacker calls `EcrecoverAuthorizer.setIsAuthorized(auth, sig_by_attacker)`:
   - Deadline check passes.
   - Nonce check passes (`nonce[A]` is consumed and incremented).
   - `ecrecover` returns `attacker`.
   - `isAuthorized[A][attacker] == true` → the `Unauthorized()` revert is **not triggered**.
   - `midnight.setIsAuthorized(attacker2, true, A)` executes → `isAuthorized[A][attacker2] = true`.
4. A calls `midnight.setIsAuthorized(attacker, false, A)` → `isAuthorized[A][attacker] = false`.
5. `isAuthorized[A][attacker2]` remains `true`. Attacker2 retains full authorization over A's position (borrow, repay, withdraw, set consumed, grant further authorizations, etc.).

**Why existing checks fail:** The nonce is per-`authorizer` (`nonce[A]`), not per-signer, so it does not prevent a delegate from consuming A's nonce to register a new sub-delegate. There is no check that the signer is the authorizer themselves when the action is to expand the authorization set. There is no cascade-revocation mechanism anywhere in the protocol.

### Impact Explanation

`attacker2` retains `isAuthorized[A][attacker2] = true` indefinitely after A revokes the original attacker. This gives attacker2 the ability to act on A's behalf for any authorized operation: `take`, `repay`, `withdraw`, `setConsumed`, `setIsAuthorized` (granting further delegates), and any other `onBehalf`-gated function. A has no way to discover or enumerate all sub-authorizations granted by the attacker, and revoking the attacker does not help.

### Likelihood Explanation

Preconditions require only that A has at some point authorized the attacker — a normal, expected usage pattern (e.g., authorizing a smart contract, a relayer, or a trading bot). The attacker needs no special privileges beyond being authorized. The attack is executable in a single transaction before A revokes, and the resulting state is permanent. It is repeatable: the attacker can pre-sign multiple `Authorization` structs for different `attacker2` addresses before A revokes.

### Recommendation

Restrict `EcrecoverAuthorizer.setIsAuthorized` so that only the `authorizer` themselves (i.e., `signer == authorization.authorizer`) may sign authorization messages. Remove the `IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer)` branch from the signer check entirely. Delegates who need to manage authorizations on behalf of a user should do so by calling `Midnight.setIsAuthorized` directly (where the caller's own `msg.sender` identity is checked), not by signing EIP-712 messages that impersonate the authorizer.

```solidity
// Replace lines 33–36 with:
require(signer == authorization.authorizer, Unauthorized());
```

### Proof of Concept

```solidity
// Foundry unit test
function testDelegateGrantsIrrevocableSubAuth() public {
    // Setup: A authorizes attacker
    vm.prank(A);
    midnight.setIsAuthorized(attacker, true, A);
    vm.prank(A);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, A);

    // Attacker signs Authorization(authorizer=A, authorized=attacker2, isAuthorized=true)
    // using attacker's own private key
    Authorization memory auth = Authorization({
        authorizer: A,
        authorized: attacker2,
        isAuthorized: true,
        nonce: ecrecoverAuthorizer.nonce(A),
        deadline: block.timestamp + 1 days
    });
    Signature memory sig = signWith(auth, attackerPrivKey); // attacker's key, not A's

    // Attacker submits — passes because isAuthorized[A][attacker] == true
    ecrecoverAuthorizer.setIsAuthorized(auth, sig);
    assertTrue(midnight.isAuthorized(A, attacker2));

    // A revokes attacker
    vm.prank(A);
    midnight.setIsAuthorized(attacker, false, A);
    assertFalse(midnight.isAuthorized(A, attacker));

    // ASSERTION: attacker2 still authorized — invariant violated
    assertTrue(midnight.isAuthorized(A, attacker2)); // passes, proving the bug
}
```

Expected: the final `assertTrue` passes, demonstrating that revoking the delegate leaves a residual authorization. A correct implementation would require this assertion to fail (i.e., `attacker2` should not be authorized after `attacker` is revoked, because the sub-authorization should never have been grantable by a delegate in the first place).

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

**File:** src/Midnight.sol (L731-734)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
```
