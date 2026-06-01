Audit Report

## Title
Authorized Delegate Can Grief Authorizer's EcrecoverAuthorizer Nonce, Invalidating Pending Off-Chain Authorizations — (`src/periphery/EcrecoverAuthorizer.sol`)

## Summary
In `EcrecoverAuthorizer.setIsAuthorized`, the authorizer's nonce is incremented unconditionally at line 26 before the signer identity is verified. Because the signer check at lines 33–36 accepts any address for which `IMidnight.isAuthorized(authorization.authorizer, signer)` returns `true`, any authorized delegate of the victim can submit a self-signed `Authorization` struct naming the victim as `authorizer`, consuming the victim's nonce and permanently invalidating any pending victim-signed authorizations at that nonce. The attacker can repeat this indefinitely, blocking the victim from using the EcrecoverAuthorizer path entirely.

## Finding Description
**Root cause:** `src/periphery/EcrecoverAuthorizer.sol` line 26 increments `nonce[authorization.authorizer]` before the signer check at lines 33–36:

```solidity
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce()); // nonce consumed unconditionally

// ... digest computation ...

address signer = ecrecover(digest, signature.v, signature.r, signature.s);
require(signer != address(0), InvalidSignature());
require(
    signer == authorization.authorizer
        || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer), // delegate accepted
    Unauthorized()
);
```

The nonce is consumed for `authorization.authorizer` regardless of whether the signer is the authorizer themselves or merely an authorized delegate. The `isAuthorized` mapping in `Midnight.sol` is a flat `mapping(address authorizer => mapping(address authorized => bool))` (line 192), so any address the victim has ever authorized on Midnight satisfies the delegate check.

**Exploit flow:**

1. Preconditions: `isAuthorized[victim][attacker] == true` and `isAuthorized[victim][EcrecoverAuthorizer] == true` on Midnight; victim has a pending off-chain signed `Authorization{authorizer=victim, ..., nonce=N}`.
2. Attacker constructs `Authorization{authorizer=victim, authorized=<any third address>, isAuthorized=<any>, nonce=N, deadline=<future>}` and signs it with the **attacker's own key**.
3. Attacker calls `EcrecoverAuthorizer.setIsAuthorized(auth, attackerSig)`.
4. Line 26: `N == nonce[victim]` → passes; `nonce[victim]` becomes `N+1`.
5. Line 34: `signer == victim` → false; `isAuthorized[victim][attacker]` → **true** → passes.
6. Line 46–47: `IMidnight.setIsAuthorized(authorized, isAuthorized, victim)` executes successfully.
7. Victim's nonce is now `N+1`; any pending victim-signed authorization at nonce `N` reverts with `InvalidNonce`.

The attacker sets `authorized` to a third address (not themselves), preserving their own authorization, and can repeat for nonces `N+1`, `N+2`, etc.

**Why existing checks fail:** The `testEcrecoverAuthorizerInvalidSignature` test (line 88–97 of `test/SetIsAuthorizedWithSigTest.sol`) only verifies that a completely unauthorized signer is rejected. It does not test the case where the signer is an authorized delegate of the authorizer. The nonce check at line 26 does not distinguish between the authorizer signing and a delegate signing.

## Impact Explanation
An attacker holding any Midnight authorization from the victim can grief the victim's `EcrecoverAuthorizer` nonce sequence indefinitely. Any off-chain signed authorizations the victim has distributed (e.g., to revoke a malicious operator or grant a new one) are rendered permanently invalid at their original nonce. The victim is forced to fall back to direct on-chain `Midnight.setIsAuthorized` calls. If the victim is an EOA who has pre-signed and distributed multiple authorization messages, all of those messages are invalidated simultaneously. The attacker can also use the consumed nonce to execute an unintended authorization change on the victim's behalf (e.g., granting a new address authorization), since `IMidnight.setIsAuthorized` is called with the attacker's chosen `authorized` and `isAuthorized` values.

## Likelihood Explanation
Preconditions are common: any user who has authorized an operator (keeper, liquidation bot, ratifier) on Midnight is vulnerable. The attacker only needs to be one of those authorized addresses. The attack requires no capital, is repeatable every block, and can front-run any victim-submitted `setIsAuthorized` transaction. The Midnight protocol documentation (lines 101–110 of `Midnight.sol`) explicitly notes that authorized accounts can authorize other accounts on behalf of the user, making this a known-but-unmitigated trust boundary.

## Recommendation
Move the nonce increment to after the signer verification, so that a failed authorization check reverts without consuming the nonce. Additionally, consider whether delegate-signed authorizations should consume the authorizer's nonce at all — if the intent is that only the authorizer's own signature should be accepted for nonce-consuming operations, restrict the signer check to `signer == authorization.authorizer` only, and require delegates to use a separate flow (e.g., direct `Midnight.setIsAuthorized`).

Minimal fix:

```solidity
function setIsAuthorized(Authorization memory authorization, Signature calldata signature) external {
    require(block.timestamp <= authorization.deadline, Expired());
    require(authorization.nonce == nonce[authorization.authorizer], InvalidNonce());

    // ... digest computation ...
    address signer = ecrecover(digest, signature.v, signature.r, signature.s);
    require(signer != address(0), InvalidSignature());
    require(
        signer == authorization.authorizer
            || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
        Unauthorized()
    );

    nonce[authorization.authorizer]++; // increment only after successful auth check
    // ...
}
```

## Proof of Concept
```solidity
function testDelegateCanGriefNonce() public {
    address victim = borrower;
    address attacker = otherLender; // attacker has been authorized by victim

    // Setup: victim authorizes EcrecoverAuthorizer and attacker on Midnight
    vm.prank(victim);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);
    vm.prank(victim);
    midnight.setIsAuthorized(attacker, true, victim);

    // Victim signs an off-chain authorization at nonce 0
    Authorization memory victimAuth = makeAuthorization(victim, lender, true);
    Signature memory victimSig = signAuthorization(victimAuth, victim);

    // Attacker constructs a different authorization at nonce 0, signed by attacker
    Authorization memory attackerAuth = Authorization({
        authorizer: victim,
        authorized: address(0xdead), // some third address
        isAuthorized: true,
        nonce: 0,
        deadline: block.timestamp + 1 days
    });
    Signature memory attackerSig = signAuthorization(attackerAuth, attacker);

    // Attacker submits first, consuming victim's nonce 0
    vm.prank(attacker);
    ecrecoverAuthorizer.setIsAuthorized(attackerAuth, attackerSig);
    assertEq(ecrecoverAuthorizer.nonce(victim), 1);

    // Victim's signed message at nonce 0 is now invalid
    vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
    ecrecoverAuthorizer.setIsAuthorized(victimAuth, victimSig);
}
```