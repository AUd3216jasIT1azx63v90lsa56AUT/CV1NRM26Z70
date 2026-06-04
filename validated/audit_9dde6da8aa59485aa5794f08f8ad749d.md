### Title
Authorized Account Can Grant Persistent Sub-Authorizations That Survive Revocation - (File: src/Midnight.sol)

### Summary

`setIsAuthorized` in `Midnight.sol` allows any currently-authorized account to grant authorization to additional accounts on behalf of the user. When the user revokes the original authorized account, sub-authorizations it created remain active. This is the direct analog of the DelegateToken M-01 finding: a delegated party can plant a persistent backdoor that survives revocation of the original delegation.

### Finding Description

The authorization check in `setIsAuthorized` is:

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
}
``` [1](#0-0) 

The check `isAuthorized[onBehalf][msg.sender]` passes for any currently-authorized account, meaning Bob (authorized by Alice) can call `setIsAuthorized(Charlie, true, Alice)` and write `isAuthorized[Alice][Charlie] = true`. When Alice later calls `setIsAuthorized(Bob, false, Alice)`, only `isAuthorized[Alice][Bob]` is cleared. `isAuthorized[Alice][Charlie]` is untouched.

Charlie can then call any `onBehalf`-gated function:

- `withdraw(market, units, Alice, attacker)` — drains Alice's credit [2](#0-1) 
- `withdrawCollateral(market, index, assets, Alice, attacker)` — drains Alice's collateral [3](#0-2) 
- `setIsAuthorized(anyone, true, Alice)` — grants further authorizations [1](#0-0) 
- `setConsumed(group, type(uint256).max, Alice)` — cancels all of Alice's offers [4](#0-3) 

The protocol's own NatSpec acknowledges this behavior but frames it only as a consideration, not a mitigated risk:

> *"authorized accounts can authorize other accounts on behalf of the user"* [5](#0-4) 

The same sub-authorization path is reachable through `EcrecoverAuthorizer.setIsAuthorized`, which checks `IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer)` — so a signed message from Bob (while still authorized) can plant Charlie via the off-chain signature path as well. [6](#0-5) 

### Impact Explanation

A user who revokes an authorized account (e.g., a DeFi aggregator, a compromised smart contract) believes they have fully cut off that account's access. In reality, any sub-authorization that account created before revocation remains live. An attacker controlling a sub-authorized address can call `withdraw` or `withdrawCollateral` to steal all of the user's credit and collateral with no further preconditions. Impact is direct, complete loss of user funds.

### Likelihood Explanation

Users routinely authorize smart contracts (bundlers, aggregators, bots) to manage their positions. A malicious or compromised authorized contract can plant a sub-authorization atomically in the same transaction it is first used, before the user has any opportunity to react. The user has no on-chain mechanism to enumerate all `isAuthorized[Alice][*]` entries, so they cannot know a backdoor was planted. Revocation of the original contract gives a false sense of security.

### Recommendation

Revoking an authorization should not automatically cascade (that would be expensive and complex), but the protocol should prevent authorized accounts from granting further authorizations on behalf of the user unless the user explicitly opts in. Concretely, tighten the `setIsAuthorized` check so that only `onBehalf == msg.sender` (the user themselves) can modify the authorization mapping for their own account:

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender, Unauthorized()); // only self
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    ...
}
```

If delegated authorization management is intentionally desired, introduce a separate, explicitly-scoped permission bit (e.g., `canSubAuthorize`) so users can grant position-management rights without also granting the ability to create new authorizations.

### Proof of Concept

```
1. Alice calls: midnight.setIsAuthorized(Bob, true, Alice)
   → isAuthorized[Alice][Bob] = true

2. Bob (now authorized) calls: midnight.setIsAuthorized(Charlie, true, Alice)
   → isAuthorized[Alice][Charlie] = true
   (passes because isAuthorized[Alice][Bob] == true)

3. Alice discovers Bob is malicious and calls:
   midnight.setIsAuthorized(Bob, false, Alice)
   → isAuthorized[Alice][Bob] = false
   → isAuthorized[Alice][Charlie] = true  ← UNCHANGED

4. Charlie calls: midnight.withdraw(market, aliceCredit, Alice, attacker)
   → passes: isAuthorized[Alice][Charlie] == true
   → Alice's entire credit balance is transferred to attacker

5. Charlie calls: midnight.withdrawCollateral(market, 0, aliceCollateral, Alice, attacker)
   → passes: isAuthorized[Alice][Charlie] == true
   → Alice's collateral is transferred to attacker
```

Steps 1–2 can be executed atomically in a single transaction by a malicious authorized contract, making the window for Alice to prevent step 4–5 effectively zero.

### Citations

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

**File:** src/Midnight.sol (L481-482)
```text
    function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L555-556)
```text
    ) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L723-724)
```text
    function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
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

**File:** src/periphery/EcrecoverAuthorizer.sol (L33-47)
```text
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
```
