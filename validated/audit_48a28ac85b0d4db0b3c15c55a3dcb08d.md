Audit Report

## Title
Authorized agent can consume victim's EcrecoverAuthorizer nonce without consent, invalidating pending pre-signed authorizations - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` keys its nonce on `authorization.authorizer` but accepts signatures from any address for whom `IMidnight.isAuthorized(authorizer, signer)` returns true. An attacker holding an active Midnight authorization from a victim can self-sign a crafted `Authorization` struct naming the victim as `authorizer` with the victim's current nonce, submit it, and permanently invalidate any pending pre-signed authorization the victim had distributed to a relayer.

## Finding Description
**Exact code path — `src/periphery/EcrecoverAuthorizer.sol` lines 24–48:**

```solidity
// Line 26: nonce consumed for authorization.authorizer (victim)
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());

// Lines 33–36: signer validity — accepts any Midnight-authorized agent
require(
    signer == authorization.authorizer
        || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);

// Line 46–47: state change executed under victim's identity
IMidnight(MIDNIGHT)
    .setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
```

**Root cause:** The nonce at line 26 is keyed on `authorization.authorizer`, but the signer check at lines 33–36 accepts any address for whom `isAuthorized[victim][signer]` is true in Midnight. There is no requirement that the signer be the authorizer themselves when the purpose is nonce consumption on the authorizer's behalf. An authorized agent can therefore produce a valid signature over an `Authorization` struct that names the victim as `authorizer` with the victim's current nonce `N`, consuming it without the victim's specific consent.

**Exploit flow:**
1. Victim pre-signs `Authorization(authorizer=victim, authorized=X, isAuthorized=true, nonce=N, deadline=T)` and hands it to a relayer.
2. Attacker (holding `isAuthorized[victim][attacker] == true` in Midnight) constructs `Authorization(authorizer=victim, authorized=victim, isAuthorized=true, nonce=N, deadline=T')` and signs it with their own key.
3. Attacker calls `EcrecoverAuthorizer.setIsAuthorized(auth, sig)`.
4. Line 26 passes: `N == nonce[victim]`, nonce incremented to `N+1`.
5. Lines 33–36 pass: `ecrecover(...)` returns attacker's address; `isAuthorized[victim][attacker]` is true in Midnight.
6. Line 47 executes: `Midnight.setIsAuthorized(victim, true, victim)` — a no-op state change (`isAuthorized[victim][victim] = true`).
7. Victim's pre-signed authorization with nonce `N` now reverts with `InvalidNonce()` when submitted by the relayer.

**Why existing checks fail:** The `Unauthorized()` guard was designed to prevent strangers from acting on behalf of an authorizer. It does not distinguish between "the authorizer consented to this specific nonce consumption" and "the signer happens to be an authorized agent." Authorized agents can already call `Midnight.setIsAuthorized` directly (confirmed at `src/Midnight.sol` line 731–735); the `isAuthorized` branch in `EcrecoverAuthorizer` adds no legitimate functionality but does introduce the nonce-consumption side effect.

## Impact Explanation
Any pending pre-signed `Authorization` with nonce `N` that the victim distributed — intended to enable a counterparty to call `take`, `repay`, `withdraw`, `liquidate`, or `claimFee` on their behalf via `EcrecoverAuthorizer` — is permanently invalidated at the cost of a single transaction. The victim must re-sign and redistribute a new authorization. The attacker can repeat this every time the victim issues a new pre-signed authorization, creating a sustained, low-cost denial-of-service against all signature-gated market actions routed through `EcrecoverAuthorizer`. No funds are directly stolen, but critical time-sensitive operations (e.g., liquidation avoidance, repayment via relayer) can be blocked indefinitely.

## Likelihood Explanation
**Preconditions:**
- Attacker must hold `isAuthorized[victim][attacker] == true` in Midnight. This is a realistic scenario: users authorize relayers, smart contracts (e.g., `EcrecoverRatifier`), callback contracts, or trading bots as part of normal protocol usage. The Midnight.sol natspec at lines 101–110 explicitly acknowledges that authorized accounts can act through contracts that re-use Midnight's authorization mapping (e.g., authorizers).
- Victim must have a pending pre-signed authorization in circulation (e.g., a signed permit handed to a relayer).

**Feasibility:** Single transaction, no capital required, no oracle manipulation, no flash loan. Repeatable indefinitely as long as the authorization relationship persists. The attacker does not need to front-run; they only need to submit before the relayer does.

## Recommendation
Remove the `isAuthorized` branch from `EcrecoverAuthorizer.setIsAuthorized`. Require the signer to always be `authorization.authorizer`:

```solidity
require(signer == authorization.authorizer, Unauthorized());
```

Authorized agents who need to manage authorizations on behalf of a user can already call `Midnight.setIsAuthorized` directly (line 731–735 of `src/Midnight.sol`) without going through `EcrecoverAuthorizer`. The `isAuthorized` branch in `EcrecoverAuthorizer` provides no additional legitimate functionality and is the sole source of the nonce-consumption vulnerability.

## Proof of Concept
**Minimal Foundry test outline:**
1. Deploy `Midnight` and `EcrecoverAuthorizer`.
2. Have `victim` call `Midnight.setIsAuthorized(attacker, true, victim)` — granting attacker authorization.
3. `victim` signs `Authorization(authorizer=victim, authorized=relayTarget, isAuthorized=true, nonce=0, deadline=T)` and gives it to a relayer (off-chain).
4. `attacker` constructs `Authorization(authorizer=victim, authorized=victim, isAuthorized=true, nonce=0, deadline=T')`, signs it with attacker's key, and calls `EcrecoverAuthorizer.setIsAuthorized(auth, sig)`.
5. Assert `EcrecoverAuthorizer.nonce(victim) == 1`.
6. Relayer attempts to submit victim's original pre-signed authorization — assert it reverts with `InvalidNonce()`. [1](#0-0) [2](#0-1) [3](#0-2)

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
