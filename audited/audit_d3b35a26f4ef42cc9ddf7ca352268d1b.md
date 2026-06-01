### Title
Permissionless Submission Enables Premature Revocation of Signed Authorization - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts any `msg.sender` and enforces only an upper-bound deadline, not a lower-bound "not-before" time. Any party who obtains a signed `Authorization{isAuthorized=false, nonce=N}` can submit it the moment the authorizer's nonce reaches `N`, regardless of when the authorizer intended to activate the revocation.

### Finding Description
The full code path in `EcrecoverAuthorizer.setIsAuthorized` is: [1](#0-0) 

The only temporal guard is:

```solidity
require(block.timestamp <= authorization.deadline, Expired());
```

There is no `validFrom` / "not-before" field in the `Authorization` struct: [2](#0-1) 

The nonce check `authorization.nonce == nonce[authorization.authorizer]++` is sequential and exact — it does not prevent early submission; it only prevents replay after submission. The function is explicitly permissionless, as confirmed by the existing test: [3](#0-2) 

**Exploit flow:**
1. Authorizer signs `Authorization{authorized=B, isAuthorized=false, nonce=N, deadline=T_future}` intending to revoke B at some future point (e.g., after a condition is met, or via a relayer).
2. The signed struct is shared with a relayer, stored off-chain, or observed from a broadcast channel.
3. The moment the authorizer's on-chain nonce reaches `N` (either because it was already `N`, or because prior authorizations were submitted), the attacker calls `ecrecoverAuthorizer.setIsAuthorized(auth, sig)` from any address.
4. The signature check passes (valid EIP-712 sig from the authorizer), the nonce check passes, the deadline check passes.
5. `IMidnight.setIsAuthorized(B, false, authorizer)` is called, revoking B immediately. [4](#0-3) 

The authorizer's intended timeline is broken with no recourse before the fact.

### Impact Explanation
Operator B loses access prematurely. Any in-flight operations that depend on B being authorized (e.g., a `take` on behalf of the authorizer, a pending `repay`, or a `setConsumed` call) will revert with `Unauthorized` after the premature revocation. The authorizer's planned delegation sequence is disrupted. The authorizer must re-authorize B, consuming an additional nonce and requiring a new on-chain transaction.

### Likelihood Explanation
The precondition — attacker possessing the signed authorization — is realistic in any meta-transaction / relayer workflow, which is the primary use case for `EcrecoverAuthorizer`. A malicious or front-running relayer, a compromised off-chain storage system, or a mempool observer can all obtain the signed struct. The nonce is sequential, so once prior authorizations are consumed, the revocation authorization becomes immediately submittable. The attack is repeatable for every future signed revocation the authorizer creates.

### Recommendation
Add a `validFrom` (not-before) timestamp field to the `Authorization` struct and enforce it in `setIsAuthorized`:

```solidity
struct Authorization {
    address authorizer;
    address authorized;
    bool isAuthorized;
    uint256 nonce;
    uint256 validFrom;   // <-- add this
    uint256 deadline;
}
```

```solidity
require(block.timestamp >= authorization.validFrom, NotYetValid());
require(block.timestamp <= authorization.deadline, Expired());
```

Alternatively, restrict submission to `msg.sender == authorization.authorizer` or a designated submitter address embedded in the struct, eliminating the permissionless submission vector for sensitive revocations.

### Proof of Concept
```solidity
function testPrematureRevocationByThirdParty() public {
    // Setup: authorizer grants B access directly
    vm.prank(borrower);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, borrower);
    vm.prank(borrower);
    midnight.setIsAuthorized(lender, true, borrower);
    assertTrue(midnight.isAuthorized(borrower, lender));

    // Authorizer signs a revocation for future use (nonce=1, current nonce=1 after above direct call)
    // Note: direct setIsAuthorized does NOT increment ecrecoverAuthorizer nonce,
    // so ecrecoverAuthorizer.nonce(borrower) == 0 here.
    Authorization memory auth = Authorization({
        authorizer: borrower,
        authorized: lender,
        isAuthorized: false,
        nonce: 0,                          // current nonce
        deadline: block.timestamp + 7 days // intended for future submission
    });
    Signature memory sig = signAuthorization(auth, borrower);

    // Attacker (otherLender) submits the revocation immediately
    vm.prank(otherLender); // unprivileged third party
    ecrecoverAuthorizer.setIsAuthorized(auth, sig);

    // B's access is revoked prematurely
    assertFalse(midnight.isAuthorized(borrower, lender));
    // Authorizer's nonce is consumed; the signed revocation cannot be reused
    assertEq(ecrecoverAuthorizer.nonce(borrower), 1);
}
```

Expected assertion: `midnight.isAuthorized(borrower, lender)` is `false` immediately after the attacker's call, before the authorizer intended to revoke access.

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L24-47)
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

        emit SetIsAuthorized(
            msg.sender,
            authorization.authorizer,
            authorization.authorized,
            authorization.isAuthorized,
            authorization.nonce
        );

        IMidnight(MIDNIGHT)
            .setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
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
