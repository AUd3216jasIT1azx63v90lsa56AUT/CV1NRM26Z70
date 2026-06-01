### Title
Nonce-Race Allows Attacker to Suppress Authorizer's Revocation Intent - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` uses a sequential, monotonically-incrementing nonce. Because two signatures sharing the same nonce are mutually exclusive and the contract imposes no ordering constraint on *who* submits, an unprivileged attacker holding a stale `isAuthorized=true` signature can submit it after the authorizer has already signed a same-nonce `isAuthorized=false` revocation, permanently consuming nonce slot N and rendering the revocation unsubmittable. The authorizer's most recently expressed off-chain intent is silently overridden.

### Finding Description
**Code path:**

`src/periphery/EcrecoverAuthorizer.sol` line 26:
```solidity
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
``` [1](#0-0) 

The check is purely sequential: the submitted `authorization.nonce` must equal the current counter, which is then post-incremented. There is no caller restriction — `testEcrecoverAuthorizerPermissionless` explicitly confirms any address may submit a valid signature. [2](#0-1) 

**Exploit flow:**

1. Authorizer A (nonce = 0) signs `auth_true = Authorization{authorizer=A, authorized=X, isAuthorized=true, nonce=0, deadline=T}` and hands it to a relayer or counterparty.
2. A changes their mind; nonce is still 0 (auth_true not yet submitted). A signs `auth_false = Authorization{..., isAuthorized=false, nonce=0, deadline=T+1}`.
3. Attacker (holding auth_true) calls `ecrecoverAuthorizer.setIsAuthorized(auth_true, sig_true)`.
   - Line 26 passes: `0 == nonce[A]++` → nonce[A] becomes 1.
   - Line 46-47 calls `IMidnight(MIDNIGHT).setIsAuthorized(X, true, A)` → `isAuthorized[A][X] = true`.
4. A (or anyone) attempts `ecrecoverAuthorizer.setIsAuthorized(auth_false, sig_false)`.
   - Line 26 fails: `0 != 1` → reverts `InvalidNonce`.
5. Final state: `isAuthorized[A][X] = true` despite A's most recent signed intent being revocation.

**Why existing checks fail:**

- The deadline check (line 25) does not help — auth_true's deadline T is still valid at submission time.
- The signature check (lines 31-36) does not help — auth_true is a legitimately signed message.
- There is no mechanism to mark a nonce slot as "superseded" or to prefer the later-signed message.
- A can recover by calling `Midnight.setIsAuthorized(X, false, A)` directly, but only after the damage is done and only if A is online. [3](#0-2) [4](#0-3) 

### Impact Explanation
An unprivileged attacker can force `isAuthorized[A][X] = true` against A's explicit revocation intent. X thereby retains the ability to act on A's behalf across all `onBehalf`-gated entry points (`withdraw`, `withdrawCollateral`, `repay`, `setConsumed`, `setIsAuthorized`, `take`) until A submits an on-chain direct revocation — defeating the purpose of the gasless revocation path.

### Likelihood Explanation
Preconditions: (1) A signed and distributed an `isAuthorized=true` authorization that has not yet been submitted; (2) A subsequently signed a same-nonce `isAuthorized=false` revocation. Both are realistic in any relayer or meta-transaction workflow. The attacker needs only to have received or intercepted the first signature. The attack is repeatable for every nonce slot and requires no special privilege or capital.

### Recommendation
Replace the single sequential nonce with a **cancellation-nonce** (a separate counter A can increment on-chain to invalidate all outstanding signatures up to that point), or add a **bitmap of invalidated nonces** so A can cancel a specific nonce slot without consuming it via submission. Alternatively, document explicitly that same-nonce revocation via a second signed message is not safe and that on-chain `Midnight.setIsAuthorized` must be used to revoke a pending gasless authorization.

### Proof of Concept
```solidity
function testNonceRaceSuppressesRevocation() public {
    // Setup: authorizer grants ecrecoverAuthorizer permission to act on their behalf
    vm.prank(authorizer);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, authorizer);

    // Step 1: authorizer signs auth_true at nonce=0
    Authorization memory authTrue = Authorization({
        authorizer: authorizer, authorized: X,
        isAuthorized: true, nonce: 0,
        deadline: block.timestamp + 1 days
    });
    Signature memory sigTrue = sign(authTrue, authorizerKey);

    // Step 2: authorizer signs auth_false at nonce=0 (intending to revoke)
    Authorization memory authFalse = Authorization({
        authorizer: authorizer, authorized: X,
        isAuthorized: false, nonce: 0,
        deadline: block.timestamp + 2 days
    });
    Signature memory sigFalse = sign(authFalse, authorizerKey);

    // Step 3: attacker submits auth_true first
    vm.prank(attacker);
    ecrecoverAuthorizer.setIsAuthorized(authTrue, sigTrue);
    assertEq(midnight.isAuthorized(authorizer, X), true);
    assertEq(ecrecoverAuthorizer.nonce(authorizer), 1);

    // Step 4: auth_false is now unsubmittable
    vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
    ecrecoverAuthorizer.setIsAuthorized(authFalse, sigFalse);

    // Invariant violated: isAuthorized is true despite authorizer's revocation intent
    assertEq(midnight.isAuthorized(authorizer, X), true);
}
```

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L24-36)
```text
    function setIsAuthorized(Authorization memory authorization, Signature calldata signature) external {
        require(block.timestamp <= authorization.deadline, Expired());
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

**File:** test/SetIsAuthorizedWithSigTest.sol (L74-86)
```text
    function testEcrecoverAuthorizerPermissionless() public {
        vm.prank(borrower);
        midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, borrower);
        Authorization memory auth = makeAuthorization(borrower, lender, true);
        Signature memory sig = signAuthorization(auth, borrower);

        // Anyone can submit — no caller auth needed
        vm.prank(otherLender);
        ecrecoverAuthorizer.setIsAuthorized(auth, sig);

        assertEq(midnight.isAuthorized(borrower, lender), true);
        assertEq(ecrecoverAuthorizer.nonce(borrower), 1);
    }
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```
