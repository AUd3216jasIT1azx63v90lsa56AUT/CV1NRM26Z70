Audit Report

## Title
Nonce-Race Front-Run on `EcrecoverAuthorizer.setIsAuthorized` Enables Unauthorized Withdrawal - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` is fully permissionless and encodes `isAuthorized` into the EIP-712 signed digest, making a grant and a revocation for the same nonce two cryptographically distinct but independently valid messages. An attacker holding a victim-signed grant for nonce=N can front-run the victim's mempool revocation (also nonce=N) by submitting the grant first, consuming the nonce and causing the revocation to revert with `InvalidNonce()`. The attacker remains authorized and can immediately call `Midnight.withdraw` to drain the victim's withdrawable loan-token balance.

## Finding Description

**Permissionless entry point — `msg.sender` is never checked:** [1](#0-0) 

**Nonce consumed atomically on first use:** [2](#0-1) 

**`isAuthorized` is a field in the signed struct, making grant and revocation two distinct valid digests for the same nonce:** [3](#0-2) 

**Digest computed over the full struct including `isAuthorized`:** [4](#0-3) 

**Signature check only requires `ecrecover` to return `authorization.authorizer` — the attacker's held grant carries a genuine victim signature:** [5](#0-4) 

**Calls `Midnight.setIsAuthorized` with attacker-controlled `authorized` and `isAuthorized`:** [6](#0-5) 

**Root cause:** Because `isAuthorized` is part of the signed digest, `{victim, attacker, true, N, far}` and `{victim, attacker, false, N, ...}` are two cryptographically distinct messages that both carry a valid victim signature and both satisfy `nonce == N`. The nonce mechanism only prevents replay of the *same* message; it does not prevent two different messages from racing on the same nonce slot.

**Why the direct-revoke escape hatch also fails:** The victim can call `Midnight.setIsAuthorized` directly to set `isAuthorized[victim][attacker] = false`, but this does not consume any nonce in `EcrecoverAuthorizer`: [7](#0-6) 

After the direct revoke, the attacker can still submit the signed grant via `EcrecoverAuthorizer` (nonce=N is still unconsumed), re-setting `isAuthorized[victim][attacker] = true`. There is no mechanism to invalidate a signed grant without consuming its nonce, and nonce consumption can be front-run.

**Exploit flow:**
1. Victim signs `Authorization{authorizer=victim, authorized=attacker, isAuthorized=true, nonce=N, deadline=far}` off-chain for a relayer and shares the signature with the attacker (relayer), but the grant is never submitted.
2. Victim changes mind, signs `Authorization{authorizer=victim, authorized=attacker, isAuthorized=false, nonce=N}`, and broadcasts the revocation to the public mempool.
3. Attacker observes the revocation in the mempool and front-runs it by submitting the old grant with the victim's valid signature at higher gas.
4. Attacker's grant executes: `nonce[victim]` becomes N+1; `isAuthorized[victim][attacker] = true` in Midnight.
5. Victim's revocation reverts: `authorization.nonce == N` but `nonce[victim] == N+1` → `InvalidNonce()`.
6. Attacker calls `Midnight.withdraw(market, units, victim, attacker)`.

**Why existing checks fail:**
- `InvalidNonce()`: prevents replay of the *same* message, but not two *different* signed messages (grant vs. revoke) racing on the same nonce.
- `Unauthorized()`: the attacker's grant carries a genuine victim signature (`ecrecover` returns `victim`), so this passes.
- `Expired()`: the attacker's grant carries a far deadline, so this passes.

## Impact Explanation

Once `isAuthorized[victim][attacker]` is `true`, the attacker calls `Midnight.withdraw`: [8](#0-7) 

The authorization check passes, `_position.credit` and `_marketState.withdrawable` are decremented, and loan tokens are transferred directly to the attacker: [9](#0-8) 

This constitutes direct theft of the victim's withdrawable loan-token balance. The reduction in `_marketState.withdrawable` and `totalUnits` also reduces the pool's ability to cover legitimate lender redemptions.

## Likelihood Explanation

**Required preconditions:**
- Victim signed a grant off-chain and shared it with the attacker (standard in relayer/meta-transaction flows — the victim authorizes a relayer, then decides to revoke before the relayer submits).
- Victim attempts revocation via `EcrecoverAuthorizer` (the only path that would consume the nonce and invalidate the signed grant).
- Attacker monitors the public mempool (standard capability on any EVM chain with a public mempool).

**Feasibility:** High. The attacker needs only to submit a higher-gas transaction with the already-held valid signature. No special privileges, no cryptographic breaks, no oracle manipulation. The attack is repeatable for each new nonce if the victim continues using `EcrecoverAuthorizer` for revocations.

## Recommendation

1. **Remove `isAuthorized` from the signed digest and instead derive it from a separate on-chain flag or use separate typehashes for grant vs. revoke.** If `isAuthorized` is not in the digest, a single signed message at nonce=N can only be used for one purpose, eliminating the race.
2. **Alternatively, implement a signed-grant invalidation mechanism** (e.g., a `cancelAuthorization` function that the authorizer can call directly to burn a specific nonce without requiring a signed message), so the victim can invalidate the held grant without going through the mempool.
3. **Or enforce that only the `authorizer` themselves (i.e., `msg.sender == authorization.authorizer`) can submit revocations**, while keeping the permissionless path only for grants. This prevents the front-run on revocations.

## Proof of Concept

**Minimal manual steps:**
1. Deploy `Midnight` and `EcrecoverAuthorizer`.
2. As `victim`, sign `Authorization{authorizer=victim, authorized=attacker, isAuthorized=true, nonce=0, deadline=far}` → `sig_grant`.
3. Give `sig_grant` to `attacker` (simulating a relayer handoff). Do not submit.
4. As `victim`, sign `Authorization{authorizer=victim, authorized=attacker, isAuthorized=false, nonce=0, deadline=far}` → `sig_revoke`. Broadcast to mempool.
5. As `attacker`, submit `setIsAuthorized(grant_struct, sig_grant)` with higher gas before `sig_revoke` lands.
6. Observe: `nonce[victim] == 1`, `isAuthorized[victim][attacker] == true`.
7. Victim's `setIsAuthorized(revoke_struct, sig_revoke)` reverts with `InvalidNonce()`.
8. As `attacker`, call `Midnight.withdraw(market, units, victim, attacker)` — succeeds, draining victim's credit.

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L24-26)
```text
    function setIsAuthorized(Authorization memory authorization, Signature calldata signature) external {
        require(block.timestamp <= authorization.deadline, Expired());
        require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```

**File:** src/periphery/EcrecoverAuthorizer.sol (L28-30)
```text
        bytes32 hashStruct = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, authorization));
        bytes32 domainSeparator = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(this)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSeparator, hashStruct));
```

**File:** src/periphery/EcrecoverAuthorizer.sol (L31-36)
```text
        address signer = ecrecover(digest, signature.v, signature.r, signature.s);
        require(signer != address(0), InvalidSignature());
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

**File:** src/periphery/interfaces/IEcrecoverAuthorizer.sol (L11-17)
```text
struct Authorization {
    address authorizer;
    address authorized;
    bool isAuthorized;
    uint256 nonce;
    uint256 deadline;
}
```

**File:** src/Midnight.sol (L481-482)
```text
    function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L493-499)
```text
        _position.credit -= UtilsLib.toUint128(units);
        _marketState.withdrawable -= UtilsLib.toUint128(units);
        _marketState.totalUnits -= UtilsLib.toUint128(units);

        emit EventsLib.Withdraw(msg.sender, id, units, onBehalf, receiver, pendingFeeDecrease);

        SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```
