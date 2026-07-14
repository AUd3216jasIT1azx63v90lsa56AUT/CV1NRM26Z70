### Title
Stale Sub-Delegations Persist After Revoking an Authorized Account - (File: src/Midnight.sol)

### Summary

`Midnight.sol` allows any authorized account to grant further authorizations on behalf of the original user via `setIsAuthorized`. When a user revokes an authorized account, all sub-delegations that account previously granted on the user's behalf remain active. There is no mechanism to enumerate or bulk-revoke these stale authorizations, leaving the user's position permanently exposed to addresses they never directly authorized.

### Finding Description

The authorization system in `Midnight.sol` uses a flat mapping:

```solidity
mapping(address authorizer => mapping(address authorized => bool)) public isAuthorized;
``` [1](#0-0) 

The `setIsAuthorized` function permits any currently-authorized account to grant or revoke authorizations on behalf of the user:

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    ...
}
``` [2](#0-1) 

The code comment explicitly acknowledges this delegation chain:

> "authorized accounts can authorize other accounts on behalf of the user" [3](#0-2) 

**Exploit path:**

1. User A authorizes User B: `setIsAuthorized(B, true, A)` → `isAuthorized[A][B] = true`
2. User B (now authorized) calls `setIsAuthorized(C, true, A)` → `isAuthorized[A][C] = true`
3. User A revokes User B: `setIsAuthorized(B, false, A)` → `isAuthorized[A][B] = false`
4. `isAuthorized[A][C]` is **never touched** — it remains `true`
5. User C retains full authorization over User A's account indefinitely

Since `isAuthorized` is a plain mapping with no associated enumeration array, User A has no way to discover or bulk-revoke sub-delegations they did not directly create.

### Impact Explanation

User C retains the ability to call every authorization-gated function on behalf of User A:

- `withdraw` — drain User A's credit/loan tokens to any `receiver`
- `withdrawCollateral` — seize User A's collateral to any `receiver`
- `setIsAuthorized` — grant further authorizations on behalf of User A, creating an unbounded delegation tree
- `setConsumed` — cancel User A's active offers
- `take` — act as taker on behalf of User A, increasing User A's debt [4](#0-3) [5](#0-4) 

The result is unauthorized theft of funds and collateral from User A's position, with no on-chain recourse once the stale authorization is exploited.

### Likelihood Explanation

The scenario is realistic and low-friction:

- The protocol explicitly documents and encourages authorizing smart contracts as operators (e.g., `MidnightBundles`, `EcrecoverAuthorizer`). Such contracts may themselves grant sub-authorizations as part of their logic.
- `EcrecoverAuthorizer` already demonstrates this pattern: a user authorizes the `EcrecoverAuthorizer` contract, which then calls `setIsAuthorized` on behalf of the user to authorize a signer. [6](#0-5) 

- A user revoking a compromised or deprecated operator contract would reasonably expect all downstream authorizations to be cleaned up. The protocol provides no warning that they are not, and no tooling to enumerate them.

### Recommendation

1. **Track authorized addresses per user**: Maintain an array alongside the mapping so that all active authorizations for a given `onBehalf` address can be enumerated and bulk-revoked.
2. **Add a `revokeAll` function**: Allow a user to atomically invalidate all authorizations granted on their behalf (analogous to a nonce increment in permit-style systems).
3. **Document the risk explicitly**: At minimum, add a NatSpec warning to `setIsAuthorized` stating that revoking an authorized account does not revoke sub-delegations that account created.

### Proof of Concept

```
1. User A calls: setIsAuthorized(B, true, A)
   → isAuthorized[A][B] = true

2. User B calls: setIsAuthorized(C, true, A)
   → isAuthorized[A][C] = true  (B is authorized, so this passes)

3. User A calls: setIsAuthorized(B, false, A)
   → isAuthorized[A][B] = false
   → isAuthorized[A][C] = true  ← UNCHANGED

4. User C calls: withdraw(market, units, A, C_address)
   → Check: isAuthorized[A][C] == true → passes
   → User A's funds are transferred to C_address

User A never authorized C directly and has revoked B,
yet C retains full control over A's position.
``` [2](#0-1) [7](#0-6)

### Citations

**File:** src/Midnight.sol (L101-109)
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
```

**File:** src/Midnight.sol (L192-192)
```text
    mapping(address authorizer => mapping(address authorized => bool)) public isAuthorized;
```

**File:** src/Midnight.sol (L481-482)
```text
    function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L549-556)
```text
    function withdrawCollateral(
        Market memory market,
        uint256 collateralIndex,
        uint256 assets,
        address onBehalf,
        address receiver
    ) external {
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
