Audit Report

## Title
Authorized Agent Can Grief Victim's Pending Pre-Signed Authorization by Consuming Nonce via No-Op Re-Authorization - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` permits any address for which `IMidnight.isAuthorized(authorizer, signer)` returns `true` to craft and submit their own signed `Authorization` struct naming the victim as `authorizer`. Because the nonce is incremented on every successful call, an existing authorized agent of the victim can consume the victim's current nonce with a no-op state change, permanently invalidating any pending pre-signed authorization the victim has distributed off-chain with that nonce. The attacker can repeat this indefinitely as long as they remain an authorized agent.

## Finding Description

**Root cause:** `EcrecoverAuthorizer.setIsAuthorized` (lines 24–48) has two interacting properties that together enable the attack:

1. The nonce is incremented on every successful call at line 26:
```solidity
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```
The nonce is permanently consumed whenever the full transaction succeeds — there is no guard requiring the resulting `Midnight.setIsAuthorized` call to produce an actual state change.

2. The signer authorization check at lines 33–36 accepts any address for which `IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer)` is true — it does not restrict signers to the `authorizer` themselves:
```solidity
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

This means any authorized agent of the victim can craft their own `Authorization` struct (naming the victim as `authorizer` and themselves as `authorized`), sign it with their own key, and submit it. The signature verification passes because `ecrecover` returns the attacker's address, and the `isAuthorized` check confirms the attacker is an authorized agent of the victim.

**Exploit flow:**

Preconditions:
- `isAuthorized[victim][EcrecoverAuthorizer] == true` on Midnight (victim uses the sig-based flow).
- `isAuthorized[victim][attacker] == true` on Midnight (attacker is an authorized agent of victim).
- Victim has distributed off-chain: `Authorization(authorizer=victim, authorized=X, isAuthorized=true, nonce=N, deadline=T)`.

Steps:
1. Attacker constructs `Authorization(authorizer=victim, authorized=attacker, isAuthorized=true, nonce=N, deadline=future)`.
2. Attacker signs this struct with their own private key.
3. Attacker calls `EcrecoverAuthorizer.setIsAuthorized(auth, attackerSig)`.

Execution trace:
- Line 25: deadline passes (attacker chose a future deadline).
- Line 26: `N == nonce[victim]` → passes; `nonce[victim]` becomes `N+1`.
- Lines 28–31: digest computed over attacker-crafted struct; `ecrecover` returns attacker's address.
- Line 32: attacker `!= address(0)` → passes.
- Line 34: `signer == victim` → false; `isAuthorized[victim][attacker]` → **true** → passes.
- Line 47: `Midnight.setIsAuthorized(attacker, true, victim)` → no-op (already `true`).

Result: `nonce[victim]` is `N+1`. Any relayer or counterparty submitting the victim's pre-signed authorization with nonce `N` receives `InvalidNonce()`.

**Why existing checks fail:** The `Unauthorized` check is designed to allow authorized agents to relay the authorizer's own pre-signed messages, but it does not restrict agents from crafting and signing their own `Authorization` structs. There is no requirement that the signer be the `authorizer` themselves, and no guard preventing a no-op re-authorization from consuming the nonce.

The `Midnight.setIsAuthorized` function confirms that authorized agents can act on behalf of the authorizer:
```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
```
The protocol documentation explicitly acknowledges that authorized accounts have broad powers and that other contracts re-use Midnight's authorization mapping (Midnight.sol lines 104–108).

## Impact Explanation
Any pending pre-signed `Authorization` distributed by the victim is permanently invalidated at the cost of one transaction. The victim must re-sign and re-distribute a new authorization with the incremented nonce. The attacker can repeat this indefinitely as long as they remain an authorized agent, creating a sustained DoS on all signature-based authorization flows for the victim. This maps to "Service unavailability or severe degradation under realistic attacker input" per RESEARCHER.md.

## Likelihood Explanation
Preconditions are realistic: users routinely authorize agents (routers, relayers, bots) on Midnight, and pre-signed authorizations are the primary off-chain UX pattern for `EcrecoverAuthorizer`. The attacker need only be one of the victim's existing authorized agents. The attack costs one transaction, requires no oracle manipulation, admin access, or leaked keys, and is repeatable every time the victim issues a new pre-signed authorization. The attacker's own authorized status (`isAuthorized[victim][attacker] == true`) is sufficient to craft a no-op re-authorization targeting themselves, requiring no additional preconditions beyond the standard setup.

## Recommendation
Remove the `isAuthorized` branch from the signer check in `EcrecoverAuthorizer.setIsAuthorized`, requiring that only the `authorizer` themselves can sign an `Authorization` struct:

```solidity
require(signer == authorization.authorizer, Unauthorized());
```

This eliminates the ability for authorized agents to craft and submit their own authorization structs naming the victim as `authorizer`. Relaying is still permissionless — any address can submit a pre-signed authorization as long as the signature is from the `authorizer` themselves (the `msg.sender` check was already removed by design, as shown in `testEcrecoverAuthorizerPermissionless`).

If the intent is to allow authorized agents to sign on behalf of the authorizer, a separate mechanism (e.g., requiring the agent to also include the authorizer's countersignature) should be used to prevent no-op nonce consumption.

## Proof of Concept

Minimal Foundry test (extend `SetIsAuthorizedWithSigTest.sol`):

```solidity
function testNonceGriefByAuthorizedAgent() public {
    // Setup: victim authorizes EcrecoverAuthorizer and attacker on Midnight
    vm.startPrank(borrower); // borrower = victim
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, borrower);
    midnight.setIsAuthorized(attacker, true, borrower); // attacker is authorized agent
    vm.stopPrank();

    // Victim pre-signs Authorization(authorizer=victim, authorized=lender, isAuthorized=true, nonce=0)
    Authorization memory victimAuth = makeAuthorization(borrower, lender, true);
    Signature memory victimSig = signAuthorization(victimAuth, borrower);

    // Attacker crafts no-op: re-authorizes themselves (already true), nonce=0
    Authorization memory attackAuth = Authorization({
        authorizer: borrower,
        authorized: attacker,
        isAuthorized: true,
        nonce: 0,
        deadline: block.timestamp + 1 days
    });
    Signature memory attackSig = signAuthorization(attackAuth, attacker);

    // Attacker submits before victim's relayer
    vm.prank(attacker);
    ecrecoverAuthorizer.setIsAuthorized(attackAuth, attackSig);

    // Victim's nonce is now 1
    assertEq(ecrecoverAuthorizer.nonce(borrower), 1);

    // Victim's pre-signed authorization is now invalid
    vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
    ecrecoverAuthorizer.setIsAuthorized(victimAuth, victimSig);

    // Victim's intended authorization never went through
    assertEq(midnight.isAuthorized(borrower, lender), false);
}
```