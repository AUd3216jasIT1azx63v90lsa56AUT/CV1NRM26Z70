Audit Report

## Title
Authorized Operator Can Permanently Grief Victim's EcrecoverAuthorizer Path via Self-Revocation - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` permits any Midnight-authorized operator of a victim to submit a signed `Authorization` struct with `authorized = address(this)` and `isAuthorized = false`, revoking the `EcrecoverAuthorizer`'s own Midnight authorization on behalf of the victim. After execution, all future signature-based authorization changes for that victim through `EcrecoverAuthorizer` revert with `Unauthorized`, and the operator can repeat the attack after every on-chain recovery, creating a persistent and cheap griefing vector.

## Finding Description

**Root cause:** `EcrecoverAuthorizer.setIsAuthorized` imposes no restriction on the values of `authorization.authorized` or `authorization.isAuthorized`. The only signer check is:

```solidity
// src/periphery/EcrecoverAuthorizer.sol lines 33–36
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
``` [1](#0-0) 

This allows any Midnight-authorized operator (`signer`) to craft and sign an `Authorization` with arbitrary `authorized` and `isAuthorized` fields. The contract then unconditionally forwards the call:

```solidity
// src/periphery/EcrecoverAuthorizer.sol lines 46–47
IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
``` [2](#0-1) 

`Midnight.setIsAuthorized` checks:

```solidity
// src/Midnight.sol line 732
require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
``` [3](#0-2) 

At call time `msg.sender = address(ecrecoverAuthorizer)` and `onBehalf = victim`. Because `isAuthorized[victim][address(ecrecoverAuthorizer)]` is still `true` at the moment of the call, the check passes and the mapping is set to `false` inside the same transaction.

**Exploit flow:**
1. Attacker (a Midnight-authorized operator of victim) constructs `Authorization { authorizer: victim, authorized: address(ecrecoverAuthorizer), isAuthorized: false, nonce: nonce[victim], deadline: future }`.
2. Attacker signs the EIP-712 digest with their own key.
3. Attacker calls `EcrecoverAuthorizer.setIsAuthorized(authorization, signature)`.
4. Signer check passes (`isAuthorized[victim][attacker] == true`).
5. `Midnight.setIsAuthorized(address(ecrecoverAuthorizer), false, victim)` executes; `isAuthorized[victim][address(ecrecoverAuthorizer)]` becomes `false`.
6. All subsequent calls to `EcrecoverAuthorizer.setIsAuthorized` with `authorizer = victim` revert at the Midnight level with `Unauthorized`.

## Impact Explanation
The victim's entire signature-based authorization path through `EcrecoverAuthorizer` is frozen. Any relayer or smart-wallet flow that relies on off-chain signatures to manage Midnight authorizations for the victim stops working. Recovery requires the victim to submit a direct on-chain transaction (`Midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim)`). Because the nonce increments on each `EcrecoverAuthorizer` call, the attacker can immediately re-execute with the next nonce value after every recovery, making the griefing persistent and essentially free (only gas cost). This constitutes a severe, repeatable service-availability degradation for any victim who relies on the sig-based path.

## Likelihood Explanation
Both preconditions are the normal, documented usage pattern: (1) the victim has opted into `EcrecoverAuthorizer` by setting `isAuthorized[victim][address(ecrecoverAuthorizer)] = true`, and (2) the victim has authorized at least one operator (e.g., a smart wallet, relayer, or protocol integration). No capital, oracle access, or special privileges beyond a valid user-level operator authorization are required. The `nonce` mapping is public state, so the attacker always knows the next valid nonce. The attack is repeatable indefinitely. [4](#0-3) 

## Recommendation
Add a guard in `EcrecoverAuthorizer.setIsAuthorized` that prevents an operator (non-authorizer signer) from revoking `EcrecoverAuthorizer` itself:

```solidity
if (signer != authorization.authorizer) {
    require(
        !(authorization.authorized == address(this) && !authorization.isAuthorized),
        Unauthorized()
    );
}
```

Only the authorizer (victim) themselves should be permitted to revoke `EcrecoverAuthorizer`'s own authorization, since that action permanently disables their sig-based path. Alternatively, disallow `authorization.authorized == address(this)` entirely and require users to call `Midnight.setIsAuthorized` directly for self-referential changes. [5](#0-4) 

## Proof of Concept

**Minimal Foundry test outline:**

```solidity
// Setup
address victim = makeAddr("victim");
address operator = makeAddr("operator");
// victim authorizes ecrecoverAuthorizer and operator in Midnight
vm.prank(victim);
midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);
vm.prank(victim);
midnight.setIsAuthorized(operator, true, victim);

// Attack: operator signs Authorization revoking ecrecoverAuthorizer
Authorization memory auth = Authorization({
    authorizer: victim,
    authorized: address(ecrecoverAuthorizer),
    isAuthorized: false,
    nonce: ecrecoverAuthorizer.nonce(victim),
    deadline: block.timestamp + 1 days
});
// operator signs EIP-712 digest of auth
(uint8 v, bytes32 r, bytes32 s) = vm.sign(operatorKey, digest(auth));

vm.prank(operator);
ecrecoverAuthorizer.setIsAuthorized(auth, Signature(v, r, s));

// Assert: ecrecoverAuthorizer is no longer authorized for victim
assertFalse(midnight.isAuthorized(victim, address(ecrecoverAuthorizer)));

// Assert: subsequent sig-based call reverts
vm.expectRevert(IMidnight.Unauthorized.selector);
ecrecoverAuthorizer.setIsAuthorized(nextAuth, nextSig);
```

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L18-18)
```text
    mapping(address => uint256) public nonce;
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

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```
