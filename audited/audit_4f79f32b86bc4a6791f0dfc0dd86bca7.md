Audit Report

## Title
Authorized-Party Nonce Burn via Delegated Signature Invalidates Pending Off-Chain Authorizations — (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` permits any party already authorized by `authorization.authorizer` in Midnight to sign and submit an `Authorization` struct naming the victim as `authorizer`, consuming the victim's current nonce without the victim's knowledge or consent. Because `nonce[authorization.authorizer]` is incremented unconditionally on every successful call regardless of which branch of the authorization check is satisfied, this permanently invalidates any pending off-chain authorization the victim has distributed with that nonce. The attack is repeatable at gas cost only, enabling indefinite denial-of-service against the victim's use of `EcrecoverAuthorizer` as long as the attacker retains authorization.

## Finding Description

**Exact code path** — `src/periphery/EcrecoverAuthorizer.sol` lines 24–48:

The nonce increment at line 26 fires unconditionally on any successful call: [1](#0-0) 

The authorization guard at lines 33–36 has two branches — the authorizer signs themselves, or any party already authorized by the authorizer in Midnight signs: [2](#0-1) 

The second branch allows ATTACKER to produce a valid EIP-712 signature over an `Authorization` struct where `authorizer = A` using ATTACKER's own private key. `ecrecover` returns ATTACKER's address; `isAuthorized[A][ATTACKER] == true` satisfies the guard. There is no check that the signer is the same party as `authorization.authorizer`, and no check that the signer did not fabricate the struct. The nonce increment at line 26 is permanent on success via either branch.

**Exploit flow:**

| Step | Action |
|------|--------|
| 0 | A calls `midnight.setIsAuthorized(ecrecoverAuthorizer, true, A)` and `midnight.setIsAuthorized(ATTACKER, true, A)` (e.g., authorizing a keeper or market maker). |
| 1 | A signs off-chain: `Authorization{authorizer=A, authorized=LEGITIMATE_OPERATOR, isAuthorized=true, nonce=N, deadline=T}` and distributes the signature. |
| 2 | ATTACKER constructs: `Authorization{authorizer=A, authorized=ATTACKER, isAuthorized=true, nonce=N, deadline=T'}` and signs it with ATTACKER's own private key. |
| 3 | ATTACKER calls `ecrecoverAuthorizer.setIsAuthorized(authAttacker, sigAttacker)`. |
| 4 | Line 26: `nonce[A]` advances N → N+1. Line 34: `isAuthorized[A][ATTACKER] == true` → passes. Call succeeds. |
| 5 | `LEGITIMATE_OPERATOR` submits A's signed authorization → reverts `InvalidNonce` because `nonce[A]` is now N+1. |
| 6 | ATTACKER repeats steps 2–5 with nonce N+1 each time A re-signs, indefinitely. |

**Why existing checks fail** — The `Unauthorized` check is satisfied by the delegation branch without distinguishing "the authorizer is signing their own nonce" from "a delegate is consuming the authorizer's nonce on a struct the authorizer never produced." The nonce increment at line 26 occurs before the signature check and is permanently committed on any successful call, including those via the delegation branch.

The `Midnight.sol` comment at lines 105–108 warns generally that "authorized accounts can authorize other accounts on behalf of the user" and that "other contracts might re-use Midnight's authorization mapping": [3](#0-2) 

This warning addresses the general delegation risk but does not address the nonce-burning attack vector specific to `EcrecoverAuthorizer`. The `Midnight.setIsAuthorized` function itself has no nonce: [4](#0-3) 

The nonce mechanism is entirely local to `EcrecoverAuthorizer`, and the delegation branch creates an asymmetry: the authorizer's nonce can be consumed by a delegate signing a struct the authorizer never produced.

## Impact Explanation
A's pending off-chain signed authorization with nonce=N is permanently invalidated. `LEGITIMATE_OPERATOR` cannot submit it; A must re-sign with nonce=N+1 and redistribute. ATTACKER can repeat this each time A re-signs, making it impossible for A to successfully delegate to any third party through `EcrecoverAuthorizer` as long as ATTACKER remains authorized. A's only escape is to revoke ATTACKER's authorization via a direct on-chain call to `midnight.setIsAuthorized(ATTACKER, false, A)`, but ATTACKER can front-run that revocation with one additional nonce burn before the revocation lands. The impact is a permanent, repeatable griefing and denial-of-service of the off-chain authorization mechanism for any user who has ever granted authorization to a counterparty.

## Likelihood Explanation
All preconditions are realistic for any active protocol participant:
1. A has authorized `ecrecoverAuthorizer` in Midnight — standard setup for any `EcrecoverAuthorizer` user.
2. A has authorized ATTACKER in Midnight at any prior point (e.g., a market maker, keeper, or liquidation bot).
3. ATTACKER knows A's current nonce — publicly readable from `nonce[A]` on-chain.
4. A has a pending off-chain signed authorization — the attack invalidates it before it is submitted.

The attack requires no privileged access beyond what A voluntarily granted. It is repeatable with zero cost beyond gas. The front-running window for the revocation escape is exploitable on any chain with a public mempool.

## Recommendation
Restrict the delegation branch so that only the authorizer themselves can consume their own nonce. The simplest fix is to remove the delegation branch entirely and require `signer == authorization.authorizer`:

```solidity
require(signer == authorization.authorizer, Unauthorized());
```

If delegation is a desired feature, the nonce should be keyed to the signer rather than the authorizer, or the contract should require the authorizer's own signature for nonce-consuming operations and use a separate, non-nonce-consuming path for delegates. Alternatively, require that the signer is the authorizer and allow delegates to call `Midnight.setIsAuthorized` directly (which has no nonce).

## Proof of Concept

**Minimal Foundry test plan:**

```solidity
// Setup
address A = makeAddr("A");
address attacker = makeAddr("attacker");
address legitimateOperator = makeAddr("legitimateOperator");

// A authorizes ecrecoverAuthorizer and attacker in Midnight
vm.prank(A);
midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, A);
vm.prank(A);
midnight.setIsAuthorized(attacker, true, A);

// A signs off-chain authorization for legitimateOperator with nonce=0
Authorization memory authLegit = Authorization({
    authorizer: A,
    authorized: legitimateOperator,
    isAuthorized: true,
    nonce: 0,
    deadline: block.timestamp + 1 days
});
// ... sign with A's private key -> sigLegit

// Attacker constructs and signs a different Authorization with authorizer=A, nonce=0
Authorization memory authAttacker = Authorization({
    authorizer: A,
    authorized: attacker,
    isAuthorized: true,
    nonce: 0,
    deadline: block.timestamp + 1 days
});
// ... sign with attacker's private key -> sigAttacker

// Attacker submits first, burning nonce=0
vm.prank(attacker);
ecrecoverAuthorizer.setIsAuthorized(authAttacker, sigAttacker);
// nonce[A] is now 1

// LegitimateOperator tries to submit A's authorization -> reverts InvalidNonce
vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
ecrecoverAuthorizer.setIsAuthorized(authLegit, sigLegit);

// Attacker can repeat with nonce=1, 2, 3... indefinitely
```

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L26-26)
```text
        require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```

**File:** src/periphery/EcrecoverAuthorizer.sol (L33-36)
```text
        require(
            signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
            Unauthorized()
        );
```

**File:** src/Midnight.sol (L105-108)
```text
/// @dev When authorizing a smart-contract, one should consider:
/// - The targets/functions that the account can call. At least Midnight's functions should be considered, but other
/// contracts might re-use Midnight's authorization mapping too (e.g ratifiers and authorizers). In particular,
/// authorized accounts can authorize other accounts on behalf of the user.
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```
