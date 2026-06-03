Audit Report

## Title
Midnight-authorized delegate can advance victim's `EcrecoverAuthorizer` nonce and invalidate all pending signed authorizations - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` always increments `nonce[authorization.authorizer]` regardless of whether the signer is the authorizer (path 1) or a Midnight-authorized delegate (path 2). Any address holding a Midnight delegation from a victim can craft a self-signed `Authorization` struct naming the victim as `authorizer`, satisfy the path-2 check with their own key, and permanently advance the victim's nonce — without the victim's private key. This invalidates every pre-signed `Authorization` the victim has distributed.

## Finding Description

**Exact code path** — `src/periphery/EcrecoverAuthorizer.sol` lines 24–48:

```solidity
function setIsAuthorized(Authorization memory authorization, Signature calldata signature) external {
    require(block.timestamp <= authorization.deadline, Expired());
    require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce()); // always authorizer's nonce

    bytes32 hashStruct = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, authorization));
    bytes32 domainSeparator = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(this)));
    bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSeparator, hashStruct));
    address signer = ecrecover(digest, signature.v, signature.r, signature.s);
    require(signer != address(0), InvalidSignature());
    require(
        signer == authorization.authorizer                                          // path 1
            || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer), // path 2
        Unauthorized()
    );
    IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
}
```

**Root cause** — The nonce consumed is always `nonce[authorization.authorizer]`, but path 2 permits any address the victim has previously authorized on Midnight to be the signer. The attacker supplies `authorization.authorizer = victim`, signs the struct with their own key, and the transaction succeeds end-to-end: the nonce check passes (attacker reads the current public nonce), the signature check passes via path 2, and the victim's nonce is permanently incremented.

**Exploit flow:**
1. Victim `V` has previously called `midnight.setIsAuthorized(attacker, true, V)` for any legitimate purpose (relayer, keeper, bundler) — the normal operating state described in `src/Midnight.sol` lines 101–110.
2. Victim distributes off-chain signed `Authorization(authorizer=V, authorized=X, isAuthorized=true, nonce=N)` to legitimate agents.
3. Attacker reads `ecrecoverAuthorizer.nonce(V)` → `N`.
4. Attacker constructs `Authorization(authorizer=V, authorized=attacker, isAuthorized=true, nonce=N, deadline=block.timestamp+1)` and signs it with their own key.
5. Attacker calls `EcrecoverAuthorizer.setIsAuthorized(auth, attackerSig)`.
6. Nonce check: `N == nonce[V]++` → passes; `nonce[V]` becomes `N+1`.
7. Signature check: `signer = attacker`, `isAuthorized(V, attacker) = true` → passes via path 2.
8. Transaction succeeds. All victim's pre-signed authorizations carrying `nonce=N` now revert with `InvalidNonce`.
9. Attacker repeats with `nonce=N+1`, `N+2`, … keeping the victim's nonce perpetually ahead.

**Why existing checks fail** — The `Unauthorized()` guard is satisfied by path 2 using the attacker's own Midnight delegation. There is no requirement that the signer must equal `authorization.authorizer` when consuming the nonce. The Certora README (`certora/README.md` line 63) describes the property as "a successful call increments only the **signer's** nonce," but the Certora spec (`certora/specs/EcrecoverAuthorizer.spec` lines 12–21) and the implementation both increment `nonce[authorization.authorizer]`, not `nonce[signer]` — confirming the design intent diverges from the README description and that the nonce protection for off-chain signed authorizations is broken when path 2 is used.

## Impact Explanation
Any victim who has ever granted Midnight authorization to any address can have their `EcrecoverAuthorizer` nonce advanced at will, permanently invalidating all pending signed `Authorization` structs. This blocks every action gated behind a pre-signed EcrecoverAuthorizer delegation. Additionally, each successful attacker call executes `IMidnight.setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer)` on behalf of the victim, constituting unauthorized privilege manipulation. Both impacts are concrete, on-chain, and irreversible per transaction.

## Likelihood Explanation
The precondition — victim has authorized attacker on Midnight — is the normal operating state for any user who delegates to a relayer, keeper, or bundler, as explicitly described in `src/Midnight.sol` lines 101–110. The attack requires no special role, no leaked keys, and no governance access. It is permissionless beyond the precondition, repeatable indefinitely, automatable to front-run every legitimate submission, and costs only gas per increment.

## Recommendation
When path 2 is used (signer ≠ authorizer), the nonce that is consumed should be `nonce[signer]`, not `nonce[authorization.authorizer]`. Alternatively, restrict path 2 so that the signer must equal `authorization.authorizer` (eliminating the delegate-signer path entirely), or introduce a separate nonce namespace per `(authorizer, signer)` pair. The simplest fix consistent with the Certora README's stated invariant ("increments only the signer's nonce") is:

```solidity
// After recovering signer:
require(authorization.nonce == nonce[signer]++, InvalidNonce());
```

This requires moving the nonce check after signature recovery, and updating the Certora spec accordingly.

## Proof of Concept
Minimal Foundry test (extend `SetIsAuthorizedWithSigTest.sol`):

```solidity
function testNonceGriefAttack() public {
    // Setup: victim authorizes attacker on Midnight
    vm.prank(borrower); // borrower = victim
    midnight.setIsAuthorized(address(this), true, borrower);

    // Victim pre-signs an authorization at nonce=0
    Authorization memory victimAuth = makeAuthorization(borrower, lender, true);
    Signature memory victimSig = signAuthorization(victimAuth, borrower);

    // Attacker constructs a self-serving auth at the same nonce, signs with own key
    Authorization memory attackAuth = Authorization({
        authorizer: borrower,
        authorized: address(this),
        isAuthorized: true,
        nonce: ecrecoverAuthorizer.nonce(borrower), // = 0
        deadline: block.timestamp + 1
    });
    Signature memory attackSig = signAuthorization(attackAuth, address(this));

    // Attacker submits first — consumes nonce=0
    ecrecoverAuthorizer.setIsAuthorized(attackAuth, attackSig);
    assertEq(ecrecoverAuthorizer.nonce(borrower), 1);

    // Victim's pre-signed authorization now reverts
    vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
    ecrecoverAuthorizer.setIsAuthorized(victimAuth, victimSig);
}
```

The test demonstrates that the attacker (holding a Midnight delegation from the victim) can consume the victim's nonce and invalidate the victim's pre-signed authorization without possessing the victim's private key. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L24-48)
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

**File:** certora/README.md (L63-63)
```markdown
- [`EcrecoverAuthorizer.spec`](specs/EcrecoverAuthorizer.spec) checks signature-based authorization: a successful call increments only the signer's nonce, and an expired deadline, wrong nonce or reused nonce reverts.
```

**File:** src/Midnight.sol (L101-110)
```text
/// AUTHORIZATIONS
/// @dev All functions that change the position, consumed and authorization are accessible to the user and to
/// any account that has been authorized. Thus, to scope authorizations one should authorize a smart-contract with
/// scoped behavior.
/// @dev When authorizing a smart-contract, one should consider:
/// - The targets/functions that the account can call. At least Midnight's functions should be considered, but other
/// contracts might re-use Midnight's authorization mapping too (e.g ratifiers and authorizers). In particular,
/// authorized accounts can authorize other accounts on behalf of the user.
/// - Under which conditions the account can return CALLBACK_SUCCESS when its isRatified function is called.
/// @dev updatePosition and liquidate (for liquidatable users) also impact the position and are permissionless.
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```
