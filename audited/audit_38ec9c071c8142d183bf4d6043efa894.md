Looking at the actual code to validate each step of the exploit chain.

Audit Report

## Title
Stale Signed Authorization Replay After Direct Revocation Re-grants Operator Access — (File: `src/periphery/EcrecoverAuthorizer.sol`)

## Summary
`EcrecoverAuthorizer` maintains a `nonce` mapping that is only incremented when `EcrecoverAuthorizer.setIsAuthorized` is called. When a victim revokes an operator directly via `Midnight.setIsAuthorized`, the `EcrecoverAuthorizer` nonce is never consumed, leaving any previously signed `Authorization` at the current nonce valid and replayable by anyone until its deadline. An attacker can replay the stale signature to re-grant the operator, who can then drain the victim's credit via `withdraw`.

## Finding Description

**Root cause:**

`EcrecoverAuthorizer.setIsAuthorized` enforces a nonce check exclusively within its own storage:

```solidity
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
``` [1](#0-0) 

The `nonce` mapping lives exclusively in `EcrecoverAuthorizer` and is only mutated here. `Midnight.setIsAuthorized` only writes to `isAuthorized[onBehalf][authorized]` and emits an event — it has no knowledge of and makes no call into `EcrecoverAuthorizer.nonce`:

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
}
``` [2](#0-1) 

**Exploit flow:**

1. Victim calls `midnight.setIsAuthorized(ecrecoverAuthorizer, true, victim)` — sets `isAuthorized[victim][ecrecoverAuthorizer] = true`. `EcrecoverAuthorizer.nonce[victim] == 0`.
2. Victim signs `Authorization{authorizer=victim, authorized=operator, isAuthorized=true, nonce=0, deadline=now+1day}`.
3. Victim revokes operator directly: `midnight.setIsAuthorized(operator, false, victim)`. Sets `isAuthorized[victim][operator] = false` in Midnight but **leaves `EcrecoverAuthorizer.nonce[victim] == 0` unchanged**.
4. Attacker (before deadline) calls `EcrecoverAuthorizer.setIsAuthorized(auth, sig)` with the old signed message.
5. Nonce check: `0 == nonce[victim]++` → passes; nonce becomes 1.
6. Signature verifies against victim's key → passes.
7. Authorization check: `signer (victim) == authorization.authorizer (victim)` → passes. [3](#0-2) 
8. `midnight.setIsAuthorized(operator, true, victim)` is called; `isAuthorized[victim][ecrecoverAuthorizer] == true` (never revoked), so the `onBehalf` check passes. [4](#0-3) 
9. `isAuthorized[victim][operator] = true` is restored.
10. Operator calls `midnight.withdraw(market, units, victim, attacker)` — authorization check passes, victim's credit is drained. [5](#0-4) 

**Why existing checks fail:** The nonce check only prevents replay of a nonce already consumed *through `EcrecoverAuthorizer`*. A direct `Midnight.setIsAuthorized` revocation does not consume any nonce in `EcrecoverAuthorizer`, so the signed message at nonce 0 remains unconsumed and valid until its deadline.

## Impact Explanation
A victim who revokes an operator via the natural `Midnight.setIsAuthorized` path believes the revocation is final. An attacker holding any unexpired signed authorization from the victim can immediately re-grant the operator, who can then call `withdraw` to steal the victim's credit. This is a direct, concrete theft of user funds with no recovery path once the operator acts within the deadline window.

## Likelihood Explanation
All three preconditions are realistic for any user of the `EcrecoverAuthorizer` peripheral: the victim authorized `EcrecoverAuthorizer` in Midnight (standard setup for signed-authorization flow), the victim signed at least one `Authorization` with `isAuthorized=true` whose deadline has not yet expired, and the victim revoked the operator via `Midnight.setIsAuthorized` directly (the natural revocation path). The attack is permissionless — any address can submit the old signature — repeatable until the deadline expires, and requires no special privileges. The attacker only needs to observe the victim's prior signed authorization from mempool or event logs.

## Recommendation
When `Midnight.setIsAuthorized` is called to revoke an authorization, the `EcrecoverAuthorizer` nonce for the authorizer should be invalidated. The cleanest fix is to add a callback or hook mechanism: `EcrecoverAuthorizer` should expose an `invalidateNonce(address authorizer)` function that increments `nonce[authorizer]`, and users should call it atomically (e.g., via `multicall`) alongside any direct revocation. Alternatively, `EcrecoverAuthorizer` could track a per-user revocation epoch and require signed messages to include it, or the nonce could be stored in `Midnight` itself so that any `setIsAuthorized` call (direct or via peripheral) increments the shared nonce. The most robust fix is to move nonce management into `Midnight` so that all authorization state changes — regardless of path — consume the same nonce sequence.

## Proof of Concept
```
1. Deploy Midnight and EcrecoverAuthorizer.
2. victim calls midnight.setIsAuthorized(ecrecoverAuthorizer, true, victim).
3. victim signs Authorization{authorizer=victim, authorized=operator, isAuthorized=true, nonce=0, deadline=block.timestamp+1 days}.
4. victim calls midnight.setIsAuthorized(operator, false, victim).
   Assert: isAuthorized[victim][operator] == false.
   Assert: EcrecoverAuthorizer.nonce[victim] == 0.  // <-- still 0
5. attacker calls EcrecoverAuthorizer.setIsAuthorized(auth, sig) with the step-3 signature.
   Assert: tx succeeds (nonce 0 accepted, signature valid, ecrecoverAuthorizer still authorized).
   Assert: isAuthorized[victim][operator] == true.  // re-granted
6. operator calls midnight.withdraw(market, units, victim, attacker).
   Assert: tx succeeds, attacker receives victim's credit.
``` [6](#0-5) [2](#0-1)

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

**File:** src/Midnight.sol (L481-482)
```text
    function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external {
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
