### Title
Authorized agent can self-deauthorize via `EcrecoverAuthorizer` to grief victim's pending nonce - (`File: src/periphery/EcrecoverAuthorizer.sol`)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` permits any address that is currently authorized by the `authorization.authorizer` in Midnight to sign and submit an authorization on the authorizer's behalf. An attacker who holds such authorization can craft a self-deauthorization message (`authorized=attacker, isAuthorized=false`) signed with their own key, submit it, and thereby consume the victim's current nonce while simultaneously removing their own authorization. Any off-chain signed authorization the victim has already distributed with that nonce is permanently invalidated.

### Finding Description
**Code path:**

`EcrecoverAuthorizer.setIsAuthorized` (lines 24–48):

```
Line 26: nonce[authorization.authorizer]++   ← incremented unconditionally on success
Lines 33-36:
  require(
      signer == authorization.authorizer
      || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
      Unauthorized()
  );
``` [1](#0-0) [2](#0-1) 

The second branch of the `require` — `isAuthorized(authorization.authorizer, signer)` — allows any currently-authorized agent of the victim to act as a valid signer for an authorization whose `authorizer` field is the victim.

**Attacker-controlled inputs:**
- `authorization.authorizer = victim`
- `authorization.authorized = attacker`
- `authorization.isAuthorized = false`
- `authorization.nonce = N` (current `nonce[victim]`, readable from public state)
- `authorization.deadline` = any future timestamp
- `signature` = attacker's own ECDSA signature over the above struct

**Exploit flow:**
1. Precondition: `isAuthorized[victim][attacker] = true` (attacker was legitimately authorized by victim).
2. Victim signs and distributes `Authorization(authorizer=victim, authorized=X, isAuthorized=true, nonce=N)` off-chain.
3. Attacker reads `nonce[victim] = N` from chain.
4. Attacker constructs `Authorization(authorizer=victim, authorized=attacker, isAuthorized=false, nonce=N, deadline=T+1)` and signs it with their own key.
5. Attacker calls `EcrecoverAuthorizer.setIsAuthorized(auth, sig)`.
6. Line 26: `nonce[victim]` increments to `N+1`.
7. Lines 33–36: `signer == attacker`, `isAuthorized[victim][attacker] == true` → check passes.
8. Line 47: `Midnight.setIsAuthorized(attacker, false, victim)` → `isAuthorized[victim][attacker] = false`. [3](#0-2) 

**Why existing checks fail:**
- The nonce check (line 26) does not prevent this — the attacker uses the correct current nonce.
- The signer check (lines 33–36) explicitly allows authorized agents to sign on behalf of the authorizer, with no restriction on what they can authorize (including self-deauthorization).
- There is no check that `signer != authorization.authorized` or that the operation is not a self-deauthorization by a delegated signer. [4](#0-3) 

### Impact Explanation
Victim's pending signed authorization with nonce `N` (already distributed off-chain, e.g., to a relayer or counterparty) is permanently invalidated. The victim must re-sign with nonce `N+1`. If the attacker re-acquires authorization (victim re-authorizes them), the attack can be repeated. Any protocol action gated on a signed authorization — `take`, `repay`, `withdraw`, `withdrawCollateral`, `liquidate`, `claimFee` — can be delayed by one nonce per attack round.

### Likelihood Explanation
**Preconditions:** Attacker must hold `isAuthorized[victim][attacker] = true`. This is a normal operational state (e.g., a relayer, operator, or ratifier authorized by the victim). **Feasibility:** The attacker only needs to submit one transaction; no capital is required. The nonce is public. **Repeatability:** One-shot per authorization held; the attacker loses their own authorization. If the victim re-authorizes the attacker (e.g., in an automated system), the attack is repeatable.

### Recommendation
Add a check that a delegated signer (i.e., `signer != authorization.authorizer`) cannot submit a self-deauthorization — specifically, disallow the case where `signer == authorization.authorized && authorization.isAuthorized == false`:

```solidity
require(
    signer == authorization.authorizer
        || (
            IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer)
            && !(signer == authorization.authorized && !authorization.isAuthorized)
        ),
    Unauthorized()
);
```

Alternatively, restrict delegated signers to only grant (not revoke) authorizations, or require that only `authorization.authorizer` themselves can sign revocations.

### Proof of Concept
```solidity
function testNonceGriefByAuthorizedAgent() public {
    // Setup: victim authorizes attacker in Midnight
    vm.prank(victim);
    midnight.setIsAuthorized(attacker, true, victim);

    // Victim signs an authorization for nonce 0 (off-chain, not yet submitted)
    Authorization memory victimAuth = Authorization({
        authorizer: victim,
        authorized: someThirdParty,
        isAuthorized: true,
        nonce: 0,
        deadline: block.timestamp + 1 days
    });
    Signature memory victimSig = signAuthorization(victimAuth, victim);

    // Attacker constructs self-deauthorization at nonce 0
    Authorization memory attackAuth = Authorization({
        authorizer: victim,
        authorized: attacker,
        isAuthorized: false,
        nonce: 0,
        deadline: block.timestamp + 1 days
    });
    Signature memory attackSig = signAuthorization(attackAuth, attacker);

    // Attacker submits self-deauthorization
    vm.prank(attacker);
    ecrecoverAuthorizer.setIsAuthorized(attackAuth, attackSig);

    // Assert: nonce[victim] incremented, attacker deauthorized
    assertEq(ecrecoverAuthorizer.nonce(victim), 1);
    assertFalse(midnight.isAuthorized(victim, attacker));

    // Assert: victim's original signed auth is now invalid (wrong nonce)
    vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
    ecrecoverAuthorizer.setIsAuthorized(victimAuth, victimSig);
}
```

**Expected assertions:**
- `nonce[victim] == 1` after attacker's call
- `isAuthorized[victim][attacker] == false`
- Victim's original `nonce=0` authorization reverts with `InvalidNonce`

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L26-26)
```text
        require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```

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
