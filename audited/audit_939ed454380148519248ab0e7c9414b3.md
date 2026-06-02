Audit Report

## Title
Authorized Signer Can Unilaterally Consume Authorizer's Nonce via Self-Deauthorization - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` permits any address that `isAuthorized[authorizer][signer]` is true for to sign and submit an `Authorization` struct on behalf of the authorizer. Because no restriction prevents the signer from naming themselves as `authorization.authorized` with `isAuthorized = false`, an authorized operator (Bob) can craft a self-deauthorization that atomically consumes the authorizer's (Alice's) current nonce, permanently invalidating any pre-signed authorization Alice has already distributed at that nonce.

## Finding Description

**Root cause** — `src/periphery/EcrecoverAuthorizer.sol` lines 24–48:

```solidity
// line 26 – nonce consumed unconditionally before signer check
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());

// lines 33–36 – signer check: authorizer OR any address authorized by authorizer
require(
    signer == authorization.authorizer
        || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);

// lines 46–47 – executes whatever the struct says, no restriction on authorized == signer
IMidnight(MIDNIGHT).setIsAuthorized(
    authorization.authorized, authorization.isAuthorized, authorization.authorizer
);
```

There is no guard preventing `signer == authorization.authorized` combined with `authorization.isAuthorized = false`. The nonce increment on line 26 is permanent once the transaction succeeds.

**Preconditions:**
1. Alice has called `midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, alice)` — required for `EcrecoverAuthorizer` to act on Alice's behalf in Midnight at all.
2. Alice has called `midnight.setIsAuthorized(bob, true, alice)` — so Bob passes the signer check.
3. Alice has off-chain signed `Authorization{authorizer:alice, authorized:charlie, isAuthorized:true, nonce:0, deadline:T}` and distributed it to Charlie.

**Exploit flow:**
1. Bob constructs `Authorization{authorizer:alice, authorized:bob, isAuthorized:false, nonce:0, deadline:T2}` and signs it with his own private key.
2. Bob calls `ecrecoverAuthorizer.setIsAuthorized(auth, sig)`.
3. Line 26: `0 == nonce[alice]++` → passes; `nonce[alice]` becomes `1`.
4. Lines 33–36: `ecrecover` returns Bob; `isAuthorized[alice][bob]` is `true` (queried *before* the deauthorization executes) → passes.
5. Lines 46–47: `midnight.setIsAuthorized(bob, false, alice)` executes → Bob is deauthorized from Midnight.
6. Charlie submits Alice's pre-signed message (nonce=0): `0 == nonce[alice]` → `0 == 1` → **reverts with `InvalidNonce`**.

**Why existing checks fail:**
The signer check (lines 33–36) is satisfied because `isAuthorized[alice][bob]` is queried before the deauthorization executes on line 47. There is no guard preventing `signer == authorization.authorized` with `isAuthorized = false`. The nonce increment is unconditional and permanent.

## Impact Explanation
Alice's nonce advances from N to N+1 without her consent. Any pre-signed `Authorization` she has distributed at nonce N — to Charlie, a relayer, or a smart contract — is permanently invalidated and cannot be submitted. Alice must re-sign at nonce N+1 and redistribute. If Alice is offline, unavailable, or the signed message was embedded in a time-sensitive workflow, the authorization is lost for that window. This constitutes unauthorized state corruption (nonce manipulation) and service disruption of the gasless authorization flow.

## Likelihood Explanation
Preconditions are normal operational states: (1) Alice has authorized `EcrecoverAuthorizer` in Midnight (required to use the system at all), (2) Alice has authorized Bob (a normal delegation), and (3) Alice has a pending pre-signed authorization. Bob needs no funds, no special role beyond being authorized by Alice, and no external oracle. The attack is a single transaction, repeatable each time Alice re-authorizes Bob.

## Recommendation
Add a check preventing an authorized signer from submitting an authorization that deauthorizes themselves (or more broadly, that the signer is not the `authorization.authorized` when `isAuthorized = false`). A targeted fix:

```solidity
require(
    signer == authorization.authorizer ||
    (authorization.isAuthorized || authorization.authorized != signer),
    Unauthorized()
);
```

Alternatively, restrict delegated signers to only authorize new addresses (not deauthorize), or require that the `authorization.authorized` field cannot equal the signer when `isAuthorized = false`. A more robust fix would use a separate nonce namespace per signer rather than per authorizer, so a delegated signer's submission only consumes their own nonce.

## Proof of Concept

Minimal Foundry test extending `EcrecoverAuthorizerTest` in `test/SetIsAuthorizedWithSigTest.sol`:

```solidity
function testNonceGriefByAuthorizedSigner() public {
    // Alice authorizes EcrecoverAuthorizer in Midnight (required for system to work)
    vm.prank(borrower); // borrower = Alice
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, borrower);

    // Alice authorizes Bob (lender) in Midnight
    vm.prank(borrower);
    midnight.setIsAuthorized(lender, true, borrower); // lender = Bob

    // Alice pre-signs auth for Charlie at nonce 0
    Authorization memory aliceAuth = makeAuthorization(borrower, otherLender, true); // charlie = otherLender
    Signature memory aliceSig = signAuthorization(aliceAuth, borrower);

    // Bob crafts self-deauthorization at nonce 0 and submits it
    Authorization memory bobAuth = Authorization({
        authorizer: borrower,
        authorized: lender,   // Bob deauthorizes himself
        isAuthorized: false,
        nonce: 0,
        deadline: vm.getBlockTimestamp() + 1 days
    });
    Signature memory bobSig = signAuthorization(bobAuth, lender);

    vm.prank(lender);
    ecrecoverAuthorizer.setIsAuthorized(bobAuth, bobSig);

    // Nonce is now 1; Alice's pre-signed message at nonce 0 is invalidated
    assertEq(ecrecoverAuthorizer.nonce(borrower), 1);

    // Charlie tries to submit Alice's pre-signed message — reverts
    vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
    ecrecoverAuthorizer.setIsAuthorized(aliceAuth, aliceSig);
}
```