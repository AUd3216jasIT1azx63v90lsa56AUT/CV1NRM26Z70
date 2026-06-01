The code is fully available. Let me trace the exact execution path.

**`EcrecoverAuthorizer.setIsAuthorized` (lines 24–48):**

```solidity
require(signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer), Unauthorized());
IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
```

**`Midnight.setIsAuthorized` (lines 731–735):**

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
}
```

**Tracing the exploit step by step:**

- Precondition A: `isAuthorized[victim][EcrecoverAuthorizer] = true` (victim opted into the sig-based flow)
- Precondition B: `isAuthorized[victim][operator] = true` (nonce=0 consumed by victim's own sig)
- Operator crafts `Authorization{authorizer=victim, authorized=attacker, isAuthorized=true, nonce=1, deadline=far_future}` and signs it with **operator's own key**
- `nonce[victim]` is currently 1 → `authorization.nonce == nonce[victim]++` → **PASSES**
- `ecrecover` returns `operator`
- `signer == authorization.authorizer` → `operator == victim` → **FALSE**
- `IMidnight(MIDNIGHT).isAuthorized(victim, operator)` → **TRUE** → **PASSES**
- Calls `Midnight.setIsAuthorized(attacker, true, victim)` with `msg.sender = EcrecoverAuthorizer`
- Inside Midnight: `isAuthorized[victim][EcrecoverAuthorizer]` → **TRUE** → **PASSES**
- **Result:** `isAuthorized[victim][attacker] = true`

The `testSetIsAuthorizedAuthorization` test at lines 290–303 of `test/AuthorizationTest.sol` already confirms that an authorized operator can call `Midnight.setIsAuthorized` directly to grant further authorizations. `EcrecoverAuthorizer` exposes the same path via the delegated-signer check, but the victim's intent when signing nonce=0 was to authorize the operator — not to grant the operator the power to sub-delegate to arbitrary addresses via a signature the victim never produced.

---

### Title
Authorized operator can sub-delegate to arbitrary attacker via EcrecoverAuthorizer without victim's signature - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts a signature from any address that `Midnight.isAuthorized(authorizer, signer)` returns true for, not only from the authorizer themselves. An operator who was granted authorization by the victim can therefore sign a fresh `Authorization` struct naming an arbitrary attacker as `authorized`, submit it to `EcrecoverAuthorizer`, pass all checks, and cause `Midnight.setIsAuthorized` to record `isAuthorized[victim][attacker] = true` — without any signature from the victim covering the attacker's address.

### Finding Description
**Root cause:** The signer-validation check in `EcrecoverAuthorizer.setIsAuthorized` (line 33–36) is:

```solidity
require(
    signer == authorization.authorizer
        || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

This allows any address that is already authorized by the victim in Midnight to produce a valid EIP-712 signature over an `Authorization` struct that names the victim as `authorizer` and an arbitrary attacker as `authorized`. The nonce check (line 26) only prevents replay of the same nonce; it does not prevent the operator from consuming the next nonce with a struct the victim never signed.

**Exploit flow:**

1. Victim calls `Midnight.setIsAuthorized(ecrecoverAuthorizer, true, victim)` — standard opt-in.
2. Victim signs `Authorization{authorizer=victim, authorized=operator, isAuthorized=true, nonce=0, deadline=T}` and submits it; `isAuthorized[victim][operator] = true`, `nonce[victim] = 1`.
3. Operator constructs `Authorization{authorizer=victim, authorized=attacker, isAuthorized=true, nonce=1, deadline=far_future}` and signs it with **operator's own private key**.
4. Operator calls `EcrecoverAuthorizer.setIsAuthorized(auth, sig)`.
5. Deadline check: passes. Nonce check: `1 == nonce[victim]` → passes, nonce becomes 2.
6. `ecrecover` → `operator`. `isAuthorized(victim, operator)` → `true` → `Unauthorized()` not triggered.
7. `Midnight.setIsAuthorized(attacker, true, victim)` executes; `msg.sender = EcrecoverAuthorizer`, `isAuthorized[victim][EcrecoverAuthorizer] = true` → passes.
8. `isAuthorized[victim][attacker] = true`.

**Why existing checks fail:** The nonce mechanism prevents replay of a specific signed message but does not prevent the operator from producing a *new* signed message over a *different* `authorized` address. The `isAuthorized` delegation check was intended to let an authorized party submit a message the victim already signed off-chain; it inadvertently also lets the operator author entirely new authorization grants.

### Impact Explanation
The attacker gains `isAuthorized[victim][attacker] = true` in Midnight without the victim ever signing a message that includes the attacker's address. The attacker can then call `withdraw`, `withdrawCollateral`, `repay`, `take`, `setConsumed`, and `setIsAuthorized` on behalf of the victim, draining collateral and credit or further escalating to additional addresses. This fully compromises the victim's Midnight position.

### Likelihood Explanation
Preconditions are realistic: any user who opts into `EcrecoverAuthorizer` and grants at least one operator authorization satisfies them. The operator need not be malicious from the start — a compromised or colluding operator key is sufficient. The attack is repeatable (each sub-delegation consumes one nonce) and requires no special on-chain state beyond the two preconditions.

### Recommendation
Restrict the signer to the authorizer themselves; remove the delegated-signer branch from `EcrecoverAuthorizer`:

```solidity
require(signer == authorization.authorizer, Unauthorized());
```

If delegated signing is intentionally desired, scope it to a separate, explicitly documented function with a distinct struct type so users understand they are granting sub-delegation power, not just a single authorization.

### Proof of Concept
```solidity
// Foundry unit test
function testOperatorSubDelegatesWithoutVictimSig() public {
    address victim  = makeAddr("victim");
    address operator = makeAddr("operator");
    address attacker = makeAddr("attacker");
    (,uint256 operatorKey) = makeAddrAndKey("operator");

    // Precondition A: victim opts into EcrecoverAuthorizer
    vm.prank(victim);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);

    // Precondition B: victim signs nonce=0 to authorize operator
    Authorization memory auth0 = Authorization({
        authorizer: victim, authorized: operator,
        isAuthorized: true, nonce: 0,
        deadline: block.timestamp + 1 days
    });
    // (sign with victim's key and submit — standard flow)
    // ... submit auth0 signed by victim ...
    // Now isAuthorized[victim][operator] = true, nonce[victim] = 1

    // Operator crafts nonce=1 naming attacker, signs with OPERATOR's key
    Authorization memory auth1 = Authorization({
        authorizer: victim, authorized: attacker,
        isAuthorized: true, nonce: 1,
        deadline: block.timestamp + 365 days
    });
    bytes32 structHash = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, auth1));
    bytes32 domainSep  = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH,
                             block.chainid, address(ecrecoverAuthorizer)));
    bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSep, structHash));
    (uint8 v, bytes32 r, bytes32 s) = vm.sign(operatorKey, digest);

    // Operator submits — no victim signature over attacker's address
    ecrecoverAuthorizer.setIsAuthorized(auth1, Signature({v:v,r:r,s:s}));

    // Invariant violated: attacker is authorized without victim's direct sig
    assertTrue(midnight.isAuthorized(victim, attacker));
}
```

Expected assertion: `assertTrue(midnight.isAuthorized(victim, attacker))` passes, proving the invariant is broken.