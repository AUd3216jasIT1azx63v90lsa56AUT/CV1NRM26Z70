Audit Report

## Title
Authorized Delegate Can Perpetually Front-Run Revocations via EcrecoverAuthorizer Delegate-Signing Loop - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` permits any address already authorized in Midnight to sign a new `Authorization` struct on behalf of the `authorizer`, not just the `authorizer` itself. A malicious operator who holds an active authorization can observe a victim's revocation in the mempool, front-run it by signing a fresh re-grant consuming the same nonce, and repeat this indefinitely. A smart-contract victim with no direct `Midnight.setIsAuthorized` call path has no on-chain mechanism to revoke the operator's authorization.

## Finding Description
**Root cause — `src/periphery/EcrecoverAuthorizer.sol` lines 33–36:**

```solidity
require(
    signer == authorization.authorizer
        || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

Any `signer` for which `isAuthorized[victim][signer] == true` in Midnight can produce a valid `Authorization` struct with `authorizer = victim`. This is confirmed by the actual code at lines 33–36.

**Nonce mechanism — line 26:**

```solidity
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```

Nonces are strictly sequential and consumed on every call. An attacker who consumes nonce `N` with a re-grant forces the victim's revocation (also targeting nonce `N`) to fail with `InvalidNonce`.

**Exploit path:**

1. Victim (smart contract, no direct `Midnight.setIsAuthorized` call path) previously authorized `operator`. State: `isAuthorized[victim][operator] = true`, `nonce[victim] = 1`.
2. Victim signs `Authorization{authorizer=victim, authorized=operator, isAuthorized=false, nonce=1}` and broadcasts.
3. Attacker (controlling `operator`) observes the pending tx. Signs `Authorization{authorizer=victim, authorized=operator, isAuthorized=true, nonce=1}` with `operator`'s key. Line 34 check passes because `isAuthorized[victim][operator] == true`. Attacker front-runs: `nonce[victim]` increments to 2, authorization remains `true`.
4. Victim's revocation lands with `nonce=1`, fails `InvalidNonce`.
5. Steps 2–4 repeat for every nonce increment.

**Why existing checks are insufficient:**
- The nonce check (line 26) prevents replay of the *same* signature but does not prevent the attacker from consuming the *next* nonce with a freshly signed re-grant.
- The deadline check (line 25) is irrelevant; the attacker can use any future deadline.
- `Midnight.setIsAuthorized` (lines 731–735) allows direct revocation only when `onBehalf == msg.sender`, which requires the victim contract to have a code path calling it directly — precisely the scenario excluded by precondition 1.

## Impact Explanation
The operator retains permanent `isAuthorized[victim][operator] = true` in Midnight. With this authorization the operator can call `withdraw`, `withdrawCollateral`, `repay`, `take`, `setConsumed`, and any other `onBehalf`-gated entry point on behalf of the victim. The victim's funds (collateral, withdrawable credit, pending fees) are fully accessible to the attacker with no on-chain revocation path available, constituting a permanent fund drain and permanent loss of access control. This is a **Critical** severity impact: direct theft of assets and permanent loss of access control.

## Likelihood Explanation
Required preconditions:
1. Victim is a smart contract whose code has no direct call to `Midnight.setIsAuthorized` — realistic for protocol integrations, smart-contract wallets, or multisigs that route all Midnight interaction through `EcrecoverAuthorizer`.
2. Victim signed at least one `isAuthorized=true` authorization and the attacker controls the operator key (the attacker *is* the operator).
3. Attacker can sign new `Authorization` structs with the operator key.

All three preconditions require no protocol-level privilege (governance, strategist, admin). The attacker is a user-level authorized operator abusing a valid protocol flow. The front-running loop is repeatable every block at negligible gas cost, and MEV infrastructure makes same-block front-running trivially reliable.

## Recommendation
Restrict the signer check in `EcrecoverAuthorizer.setIsAuthorized` so that only `authorization.authorizer` itself (i.e., `signer == authorization.authorizer`) is accepted as a valid signer. Remove the `isAuthorized` delegation path from this function entirely. If delegation is desired, it should be scoped to specific actions and must not include the ability to sign further authorization changes on behalf of the authorizer, which creates the circular revocation-blocking dependency.

## Proof of Concept
**Minimal Foundry test outline:**

1. Deploy `Midnight` and `EcrecoverAuthorizer`.
2. Create `victim` (a contract with no `Midnight.setIsAuthorized` call path) and `operator` (an EOA).
3. Have `victim` call `EcrecoverAuthorizer.setIsAuthorized` with a signed `Authorization{authorizer=victim, authorized=operator, isAuthorized=true, nonce=0}` signed by `victim`'s key. Confirm `isAuthorized[victim][operator] == true`, `nonce[victim] == 1`.
4. Victim signs `Authorization{authorizer=victim, authorized=operator, isAuthorized=false, nonce=1}` (revocation).
5. Before submitting victim's tx, submit attacker's tx: `Authorization{authorizer=victim, authorized=operator, isAuthorized=true, nonce=1}` signed by `operator`'s key. Confirm it succeeds (line 34 check passes), `nonce[victim] == 2`, `isAuthorized[victim][operator] == true`.
6. Submit victim's revocation tx with `nonce=1`. Confirm it reverts with `InvalidNonce`.
7. Assert `isAuthorized[victim][operator] == true` permanently. [1](#0-0) [2](#0-1)

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L24-36)
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
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```
