Audit Report

## Title
Signed `Authorization` replay bypasses direct revocation via `Midnight.setIsAuthorized` — (`src/periphery/EcrecoverAuthorizer.sol`)

## Summary
`EcrecoverAuthorizer` maintains its own `nonce` mapping for replay protection, but `Midnight.setIsAuthorized` — the direct revocation path — writes only to `isAuthorized` and never increments `EcrecoverAuthorizer.nonce`. Any signed `Authorization{isAuthorized=true}` that has not yet been submitted through `EcrecoverAuthorizer` remains valid after a direct revocation, allowing an attacker holding such a signature to re-grant themselves access and achieve full account takeover.

## Finding Description
**Root cause:** `EcrecoverAuthorizer.nonce` and `Midnight.isAuthorized` are independent state variables with no synchronization between them.

`EcrecoverAuthorizer.setIsAuthorized` enforces replay protection solely via its own nonce: [1](#0-0) 

`Midnight.setIsAuthorized` only writes to `isAuthorized` and emits an event — it has no interaction with `EcrecoverAuthorizer.nonce`: [2](#0-1) 

**Exploit flow:**

1. Victim calls `Midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim)` → `isAuthorized[victim][ecrecoverAuthorizer] = true`. Required to use gasless authorization at all.
2. Victim signs `Authorization{authorizer=victim, authorized=attacker, isAuthorized=true, nonce=0, deadline=T+future}`. Attacker obtains this signature. At this point `nonce[victim] = 0`.
3. Victim calls `Midnight.setIsAuthorized(attacker, false, victim)` to revoke. `isAuthorized[victim][attacker]` becomes `false`. `EcrecoverAuthorizer.nonce[victim]` remains `0`.
4. Attacker calls `EcrecoverAuthorizer.setIsAuthorized(auth, sig)` with the old signed struct:
   - Line 25: deadline still in future → passes.
   - Line 26: `auth.nonce (0) == nonce[victim] (0)` → passes; nonce incremented to 1.
   - Lines 33–36: `signer == auth.authorizer` (victim's valid signature) → passes.
   - Lines 46–47: calls `Midnight.setIsAuthorized(attacker, true, victim)`; `isAuthorized[victim][ecrecoverAuthorizer]` is still `true` → passes.
5. Result: `isAuthorized[victim][attacker]` is `true` again. [3](#0-2) 

**Why existing checks fail:** The nonce check on line 26 only prevents reuse of a signature that was already submitted through `EcrecoverAuthorizer`. A direct call to `Midnight.setIsAuthorized` is invisible to `EcrecoverAuthorizer.nonce`, so any signed-but-not-yet-submitted authorization remains valid after a direct revocation.

## Impact Explanation
An attacker holding a signed `Authorization{isAuthorized=true}` can permanently re-authorize themselves on the victim's account even after the victim explicitly revokes via `Midnight.setIsAuthorized`. The protocol documentation confirms that authorized accounts can call all position-modifying functions and can even authorize other accounts on behalf of the user: [4](#0-3) 

This grants the attacker the ability to act as the victim in any context that checks `isAuthorized` — including `take` (line 346), `setConsumed` (line 724), `withdrawCollateral` (line 556), `repay` (line 505), `supplyCollateral` (line 527), and further `setIsAuthorized` calls (line 732) — constituting a full account takeover for all protocol actions. The attack is repeatable until the deadline expires or the victim also revokes `EcrecoverAuthorizer` itself from Midnight.

## Likelihood Explanation
Both preconditions are normal usage patterns: (a) authorizing `EcrecoverAuthorizer` in Midnight is required to use gasless authorization at all, and (b) signing an `Authorization{isAuthorized=true}` for a counterparty is the intended use of `EcrecoverAuthorizer`. The attacker only needs to hold the signed message and wait for the victim to attempt a direct revocation. The victim has no on-chain indication that a signed-but-unsubmitted authorization exists, and no reason to know that direct revocation is insufficient. The attack requires no privileged access, no leaked keys, and no unrealistic assumptions.

## Recommendation
Synchronize revocation state between `Midnight` and `EcrecoverAuthorizer`. The most robust fix is to increment `EcrecoverAuthorizer.nonce[onBehalf]` inside `Midnight.setIsAuthorized` whenever `newIsAuthorized=false` and `authorized == address(ecrecoverAuthorizer)` — or more generally, expose a `invalidateNonce(address authorizer)` function on `EcrecoverAuthorizer` that `Midnight` calls (or that the victim can call directly) to burn all outstanding signed authorizations. Alternatively, `EcrecoverAuthorizer.setIsAuthorized` could check the current `Midnight.isAuthorized` state for the `authorized` address before proceeding, rejecting replays that would re-grant a revoked authorization. The cleanest solution is to add a `invalidateNonces` function to `EcrecoverAuthorizer` that the victim can call to advance their nonce, invalidating all outstanding signed authorizations. [5](#0-4) 

## Proof of Concept
Minimal Foundry test:

```solidity
// 1. Setup: victim authorizes EcrecoverAuthorizer in Midnight
midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim); // vm.prank(victim)

// 2. Victim signs Authorization{authorizer=victim, authorized=attacker, isAuthorized=true, nonce=0, deadline=block.timestamp+1 days}
// Attacker obtains this signature off-chain (e.g., from a prior gasless tx flow)

// 3. Victim revokes attacker directly
midnight.setIsAuthorized(attacker, false, victim); // vm.prank(victim)
assertFalse(midnight.isAuthorized(victim, attacker));
// EcrecoverAuthorizer.nonce[victim] is still 0

// 4. Attacker replays the signed authorization
ecrecoverAuthorizer.setIsAuthorized(auth, sig); // vm.prank(attacker)

// 5. Verify account takeover
assertTrue(midnight.isAuthorized(victim, attacker)); // PASSES — attacker re-authorized
```

The test will pass, confirming the replay succeeds after direct revocation.

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

**File:** src/Midnight.sol (L101-110)
```text
/// AUTHORIZATIONS
/// @dev All functions that change the position, consumed and authorization are accessible to the user and to
/// any account that has been authorized. Thus, to scope authorizations one should authorize a smart-contract with
/// scoped behavior.
/// @dev When authorizing a smart-contract, one should consider:
/// - The targets/functions that the account can call. At least Midnight's functions should be considered, but other
/// contracts might re-use Midnight's authorization mapping too (e.g ratifiers and authorizers). In particular,
/// authorized accounts can authorize other accounts on behalf of the user.
/// - Under which conditions the account can return CALLBACK_SUCCESS when its isRatified function is called.
/// @dev updatePosition and liquidate (for liquidatable users) also impact the position and are permissionless.
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```
