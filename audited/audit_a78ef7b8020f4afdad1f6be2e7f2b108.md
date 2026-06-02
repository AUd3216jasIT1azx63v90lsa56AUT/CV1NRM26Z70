Audit Report

## Title
ECDSA Signature Malleability Enables Nonce-Griefing Front-Run on `setIsAuthorized` - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` calls raw `ecrecover` at line 31 without enforcing a low-s constraint, allowing any observer to compute a second valid signature `(v^1, r, secp256k1_order − s)` for the same message and signer. An attacker who front-runs a victim's pending `setIsAuthorized` transaction with the malleable counterpart consumes the victim's nonce, causing the victim's original transaction to revert with `InvalidNonce`. When the victim's authorization is part of an atomic bundle (e.g., authorize-then-protect against liquidation via `MidnightBundles`), the entire bundle fails and must be re-submitted, introducing a timing gap that can be exploited repeatedly.

## Finding Description
**Exact code path:**

`EcrecoverAuthorizer.setIsAuthorized` (src/periphery/EcrecoverAuthorizer.sol, lines 24–48):

- Line 26: `require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());` — nonce is checked and incremented atomically; first caller wins.
- Line 31: `address signer = ecrecover(digest, signature.v, signature.r, signature.s);` — raw `ecrecover` with no low-s guard.
- No `msg.sender` restriction anywhere in the function; any address may call it.

**Root cause:** secp256k1 ECDSA produces two valid `(v, r, s)` pairs per `(message, key)`: the canonical `(v, r, s)` and the malleable `(v^1, r, n − s)` where `n` is the curve order. Both pass `ecrecover` and return the identical signer address. Without `require(uint256(s) <= 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0)`, both are accepted.

**Exploit flow:**
1. Victim signs `Authorization{authorizer=V, authorized=X, isAuthorized=true, nonce=N, deadline=D}` and broadcasts `setIsAuthorized(auth, sig)`.
2. Attacker observes the pending transaction, extracts `(v, r, s)`, computes `s' = n − s`, `v' = v ^ 1`.
3. Attacker submits `setIsAuthorized(auth, Signature{v', r, s'})` with higher gas.
4. Attacker's transaction executes first: `nonce[V] == N` passes, nonce becomes `N+1`, `ecrecover` returns `V`, authorization is set.
5. Victim's transaction executes: `nonce[V] == N+1` but `auth.nonce == N` → `InvalidNonce()` revert.

**Why existing checks fail:**
- `require(signer != address(0))` — both signatures recover the same non-zero address; passes.
- `require(signer == authorization.authorizer || isAuthorized(...))` — same signer recovered; passes.
- The nonce is already consumed before the victim's transaction executes.

## Impact Explanation
The victim's `setIsAuthorized` transaction reverts. If submitted as part of an atomic bundle via `MidnightBundles` (e.g., authorize a protective contract and immediately invoke it to repay debt or withdraw collateral before liquidation), the entire bundle fails. The victim must re-sign with nonce `N+1` and resubmit. The attacker can repeat the front-run for every subsequent attempt, continuously delaying execution. In a liquidation-protection scenario, this repeated delay can result in the victim's position being liquidated while they are unable to atomically authorize and invoke their protective contract. The impact is concrete griefing with potential for indirect financial loss in time-sensitive contexts.

## Likelihood Explanation
**Preconditions:** Victim broadcasts a `setIsAuthorized` transaction (routine operation). Attacker monitors the public mempool (standard, zero-cost capability). No special role, funds, or credentials required.

**Feasibility:** Computing `s' = n − s` and `v' = v ^ 1` is trivial off-chain arithmetic. The attack is repeatable for every nonce the victim attempts to use. On chains with a public mempool (Ethereum mainnet, most L2s), this is straightforwardly executable. The attacker pays gas but gains the ability to persistently grief time-sensitive operations.

## Recommendation
Add a low-s check immediately before or after the `ecrecover` call:

```solidity
uint256 SECP256K1_ORDER_HALF = 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0;
require(uint256(signature.s) <= SECP256K1_ORDER_HALF, InvalidSignature());
```

Alternatively, use OpenZeppelin's `ECDSA.recover`, which enforces the low-s constraint and reverts on malleable signatures. This ensures only one of the two possible signatures per message is accepted, eliminating the malleability vector entirely.

## Proof of Concept
**Minimal Foundry test outline:**

```solidity
function test_malleability_nonce_grief() public {
    // 1. Victim signs Authorization{authorizer=victim, authorized=X, isAuthorized=true, nonce=0, deadline=...}
    //    producing (v, r, s)
    // 2. Compute malleable sig: s2 = secp256k1_order - s; v2 = v ^ 1
    // 3. Attacker calls setIsAuthorized(auth, Signature{v2, r, s2}) — succeeds, nonce becomes 1
    // 4. Victim calls setIsAuthorized(auth, Signature{v, r, s}) — reverts with InvalidNonce()
    // 5. Assert: nonce[victim] == 1, victim's tx reverted
}
```

Both `ecrecover(digest, v, r, s)` and `ecrecover(digest, v^1, r, n-s)` return the same address, confirming the malleability. The nonce increment on step 3 is the concrete trigger for the victim's revert on step 4.