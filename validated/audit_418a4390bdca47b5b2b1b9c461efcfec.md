### Title
Authorized Accounts Can Grant Persistent Sub-Authorizations That Survive Revocation — (File: src/Midnight.sol)

### Summary

The `setIsAuthorized` function in `Midnight.sol` allows any currently-authorized account to grant further authorizations on behalf of the user. When a user revokes an authorized account, sub-authorizations that account previously granted on the user's behalf are not cleared. This mirrors the Farcaster `IdRegistry` bug: a "secondary authority" (sub-authorized account) persists after the primary authority (authorized account) is revoked, giving the attacker continued full access to the victim's position.

### Finding Description

**Root cause — `src/Midnight.sol` lines 731–735:**

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
}
```

The guard `isAuthorized[onBehalf][msg.sender]` means any account that Alice has authorized can itself call `setIsAuthorized(..., true, Alice)` to authorize a third party (Charlie) on Alice's behalf. The protocol explicitly documents this: *"authorized accounts can authorize other accounts on behalf of the user"* (line 108).

The same delegation path exists in `EcrecoverAuthorizer.sol` lines 33–34:

```solidity
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

An authorized account can sign (or have signed) an `Authorization` struct that grants Charlie access, and this is accepted by the authorizer.

**Attack path:**

1. Alice calls `setIsAuthorized(Bob, true, Alice)` — authorizes Bob (e.g., a DeFi aggregator or smart-contract wallet).
2. Bob (or a malicious/compromised version of it) calls `setIsAuthorized(Charlie, true, Alice)` — grants Charlie authorization on Alice's behalf. This can happen during normal operation or as a front-run when Bob detects Alice's pending revocation.
3. Alice calls `setIsAuthorized(Bob, false, Alice)` — revokes Bob.
4. `isAuthorized[Alice][Charlie]` remains `true`. Alice has no on-chain enumeration of sub-authorizations and no way to know Charlie exists.
5. Charlie calls `withdraw(market, units, Alice, Charlie)`, `withdrawCollateral(...)`, or `setIsAuthorized(...)` on Alice's behalf — draining her position or escalating further.

**Analogy to external report:**

| Farcaster | Midnight |
|---|---|
| FID owner | Alice (user) |
| Recovery address | Bob (authorized account) |
| `transfer()` changes owner, not recovery | `setIsAuthorized(Bob, false)` revokes Bob, not Charlie |
| Old owner calls `recover()` to seize FID | Charlie calls `withdraw/withdrawCollateral` to drain position |

### Impact Explanation

Charlie has the same authority as Alice over her position. Concretely:

- **`withdraw`** — steal Alice's credit (loan tokens at maturity).
- **`withdrawCollateral`** — steal Alice's collateral assets.
- **`setIsAuthorized`** — authorize additional accounts, making the compromise permanent and expanding the attack surface.
- **`setConsumed`** — cancel Alice's active offers, disrupting her market-making.
- **`repay`** — force-repay Alice's debt using Charlie's own funds (griefing or front-running a liquidation for profit).

The impact is **direct theft of assets** with no recovery path once collateral or credit is withdrawn.

### Likelihood Explanation

- Users routinely authorize smart contracts (bundlers, aggregators, relayers) to interact with their positions.
- A compromised or malicious authorized contract can silently call `setIsAuthorized(attacker, true, victim)` in a single transaction, before or concurrently with the victim's revocation.
- The victim has no on-chain mechanism to enumerate all accounts authorized on their behalf; `isAuthorized` is a flat mapping with no list structure.
- Front-running the revocation transaction is straightforward on any EVM chain with a public mempool.

### Recommendation

1. **Prevent sub-authorization by non-owners:** Restrict `setIsAuthorized` so that only `onBehalf == msg.sender` (the account itself) can grant new authorizations. Authorized accounts should only be able to act *within* the protocol (take, withdraw, repay, etc.) but not *extend* the authorization graph.

2. **Alternatively, add a `revokeAll` / nonce-based invalidation:** Introduce a per-user nonce or generation counter. Incrementing it invalidates all current authorizations atomically, analogous to Farcaster's `transferAndChangeRecovery`.

3. **Emit a warning in `setIsAuthorized`** when `msg.sender != onBehalf` so off-chain monitoring can alert users to unexpected sub-authorizations.

### Proof of Concept

```
1. Deploy Midnight. Alice has credit = 1000 units in market M.

2. Alice: setIsAuthorized(Bob, true, Alice)
   → isAuthorized[Alice][Bob] = true

3. Bob (tx): setIsAuthorized(Charlie, true, Alice)
   → passes: isAuthorized[Alice][Bob] == true
   → isAuthorized[Alice][Charlie] = true

4. Alice: setIsAuthorized(Bob, false, Alice)
   → isAuthorized[Alice][Bob] = false
   → isAuthorized[Alice][Charlie] = true  ← NOT cleared

5. Charlie: withdraw(M, 1000, Alice, Charlie)
   → passes: isAuthorized[Alice][Charlie] == true
   → Alice loses 1000 units; Charlie receives 1000 loan tokens
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/Midnight.sol (L101-111)
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
///
```

**File:** src/Midnight.sol (L481-483)
```text
    function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
```

**File:** src/Midnight.sol (L549-557)
```text
    function withdrawCollateral(
        Market memory market,
        uint256 collateralIndex,
        uint256 assets,
        address onBehalf,
        address receiver
    ) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
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
