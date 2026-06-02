Audit Report

## Title
Authorized Signer Can Unilaterally Consume Authorizer's Nonce via Self-Deauthorization - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` allows any address for which `IMidnight.isAuthorized(authorizer, signer)` returns `true` to sign and submit an `Authorization` struct naming themselves as `authorization.authorized` with `isAuthorized = false`. Because the nonce is incremented unconditionally before the signer check and there is no guard preventing `signer == authorization.authorized` with `isAuthorized = false`, an authorized operator (Bob) can craft a self-deauthorization that atomically consumes the authorizer's (Alice's) current nonce, permanently invalidating any pre-signed authorization Alice has already distributed at that nonce.

## Finding Description

**Root cause — `src/periphery/EcrecoverAuthorizer.sol` lines 24–48:**

```solidity
// line 26 – nonce consumed unconditionally before signer check
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());

// lines 33–36 – signer check: authorizer OR any address authorized by authorizer
require(
    signer == authorization.authorizer
        || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);

// lines 46–47 – executes whatever the struct says; no restriction on authorized == signer
IMidnight(MIDNIGHT).setIsAuthorized(
    authorization.authorized, authorization.isAuthorized, authorization.authorizer
);
```

There is no guard preventing `signer == authorization.authorized` combined with `authorization.isAuthorized = false`. The nonce increment on line 26 is permanent once the transaction succeeds.

**Preconditions (all normal operational states):**
1. Alice has called `midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, alice)` — required for `EcrecoverAuthorizer` to act on Alice's behalf in Midnight at all.
2. Alice has called `midnight.setIsAuthorized(bob, true, alice)` — so Bob passes the signer check.
3. Alice has off-chain signed `Authorization{authorizer:alice, authorized:charlie, isAuthorized:true, nonce:0, deadline:T}` and distributed it to Charlie.

**Exploit flow:**
1. Bob constructs `Authorization{authorizer:alice, authorized:bob, isAuthorized:false, nonce:0, deadline:T2}` and signs it with his own private key.
2. Bob calls `ecrecoverAuthorizer.setIsAuthorized(auth, sig)`.
3. Line 26: `0 == nonce[alice]++` → passes; `nonce[alice]` becomes `1`.
4. Lines 33–36: `ecrecover` returns Bob; `IMidnight(MIDNIGHT).isAuthorized(alice, bob)` is `true` (queried *before* the deauthorization executes on line 47) → passes.
5. Lines 46–47: `midnight.setIsAuthorized(bob, false, alice)` executes → Bob is deauthorized from Midnight.
6. Charlie submits Alice's pre-signed message (nonce=0): `0 == nonce[alice]` → `0 == 1` → **reverts with `InvalidNonce`**.

**Why existing checks fail:**
The signer check (lines 33–36) is satisfied because `isAuthorized[alice][bob]` is queried before the deauthorization executes on line 47. There is no guard preventing `signer == authorization.authorized` with `isAuthorized = false`. The nonce increment is unconditional and permanent.

## Impact Explanation
Alice's nonce advances from N to N+1 without her consent. Any pre-signed `Authorization` she has distributed at nonce N — to Charlie, a relayer, or a smart contract — is permanently invalidated and cannot be submitted. Alice must re-sign at nonce N+1 and redistribute. If Alice is offline, unavailable, or the signed message was embedded in a time-sensitive workflow, the authorization is lost for that window. This constitutes unauthorized state corruption (nonce manipulation) and service disruption of the gasless authorization flow. The attack is repeatable: each time Alice re-authorizes Bob, he can repeat the attack at the new nonce.

## Likelihood Explanation
All three preconditions are normal operational states: (1) Alice must authorize `EcrecoverAuthorizer` in Midnight to use the system at all; (2) Alice authorizing Bob is a standard delegation; (3) Alice having a pending pre-signed authorization is the core use case of the contract. Bob requires no funds, no special protocol-level role (governance/admin/strategist), and no external oracle. The attack is a single transaction executable by any address Alice has delegated to.

## Recommendation
Add a guard that prevents a signer from submitting a deauthorization naming themselves as the `authorized` address. The minimal targeted fix:

```solidity
require(
    authorization.isAuthorized || authorization.authorized != signer,
    SelfDeauthorizationForbidden()
);
```

This blocks the specific attack vector (Bob deauthorizing himself to consume Alice's nonce) while preserving the ability for authorized operators to deauthorize *other* accounts on behalf of the authorizer. Alternatively, restrict deauthorization messages (`isAuthorized = false`) to be signed only by the authorizer themselves, not by delegated signers.

## Proof of Concept

**Minimal manual steps:**
1. Deploy `Midnight` and `EcrecoverAuthorizer`.
2. Alice calls `midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, alice)`.
3. Alice calls `midnight.setIsAuthorized(bob, true, alice)`.
4. Alice signs off-chain: `Authorization{authorizer:alice, authorized:charlie, isAuthorized:true, nonce:0, deadline:T}` → gives to Charlie.
5. Bob signs: `Authorization{authorizer:alice, authorized:bob, isAuthorized:false, nonce:0, deadline:T2}` with Bob's key.
6. Bob calls `ecrecoverAuthorizer.setIsAuthorized(bobAuth, bobSig)` → succeeds; `nonce[alice]` = 1.
7. Charlie calls `ecrecoverAuthorizer.setIsAuthorized(charlieAuth, aliceSig)` → reverts with `InvalidNonce`.

**Invariant/fuzz test plan:** Fuzz `Authorization.authorized` and `Authorization.isAuthorized` for all addresses that satisfy `isAuthorized[authorizer][signer] == true`; assert that after any successful `setIsAuthorized` call, the nonce of `authorization.authorizer` has not advanced unless the resulting Midnight state change was explicitly intended by the authorizer (i.e., `authorization.authorized != signer || authorization.isAuthorized == true`).