Audit Report

## Title
Malicious delegate front-runs maker's EcrecoverAuthorizer revocation to permanently grief offer groups - (File: `src/periphery/EcrecoverAuthorizer.sol`)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts signatures from any existing delegate of the maker, allowing a malicious delegate to consume the maker's current nonce with a crafted authorization before the maker's pending revocation lands. The delegate simultaneously authorizes an attacker address, which then calls `Midnight.setConsumed(group, type(uint256).max, maker)`, irreversibly cancelling all of the maker's offers in that group.

## Finding Description

**Root cause 1 — Nonce consumed before signature verification:**

In `EcrecoverAuthorizer.setIsAuthorized`, line 26 increments `nonce[authorization.authorizer]` atomically before the signature is checked:

```solidity
require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
```

Once any transaction consuming nonce N lands, the maker's signed message using nonce N is permanently invalid — even if the consuming transaction was submitted by a delegate, not the maker. [1](#0-0) 

**Root cause 2 — Delegates can sign authorizations on behalf of the maker:**

Lines 33–36 accept a signature from any address that `IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer)` returns true for:

```solidity
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

A delegate can construct and sign an `Authorization` struct with `authorization.authorizer = maker` and arbitrary `authorized`/`isAuthorized` values, and the contract will accept it as if the maker signed it. [2](#0-1) 

**Root cause 3 — `setConsumed` with `type(uint256).max` is irreversible:**

`setConsumed` enforces strict monotonicity via `AlreadyConsumed`, and `consumed` can never decrease. Setting it to `type(uint256).max` permanently blocks every subsequent `take` on any offer in that group. [3](#0-2) 

**Full exploit path:**

| Step | Actor | Action | Effect |
|------|-------|--------|--------|
| 0 | maker | `Midnight.setIsAuthorized(ecrecoverAuthorizer, true, maker)` | EcrecoverAuthorizer becomes a delegate |
| 1 | maker | `Midnight.setIsAuthorized(delegate, true, maker)` | delegate is authorized |
| 2 | maker | Signs `Authorization{maker, delegate, false, N, deadline}`, broadcasts | Pending in mempool |
| 3 | delegate | Signs `Authorization{maker, attacker, true, N, deadline2}` | Off-chain |
| 4 | delegate | `EcrecoverAuthorizer.setIsAuthorized(auth3, sig3)` (front-run) | `nonce[maker]→N+1`, `isAuthorized[maker][attacker]=true` |
| 5 | maker | Maker's tx lands | Reverts `InvalidNonce` |
| 6 | attacker | `Midnight.setConsumed(group, type(uint256).max, maker)` | `consumed[maker][group]=type(uint256).max` — permanent |

At Step 4, `isAuthorized[maker][delegate]` is still `true` (the revocation has not landed), so the delegate's signature passes the authorization check at line 34. The maker can subsequently call `Midnight.setIsAuthorized(delegate, false, maker)` directly, but `consumed[maker][group]` is already `type(uint256).max` and cannot be lowered. [4](#0-3) 

## Impact Explanation

All of the maker's offers sharing the griefed `group` are permanently cancelled — every `take` on them reverts with `ConsumedUnits` or `ConsumedAssets`. The maker cannot recover the group budget because `setConsumed` enforces strict monotonicity. The maker must redeploy offers under a new group identifier, losing any off-chain routing or aggregator integrations that referenced the old group. This constitutes permanent, irreversible corruption of user state — a concrete in-scope impact class per RESEARCHER.md ("Permanent lock, freeze, or unrecoverable corruption of user/project state"). [5](#0-4) 

## Likelihood Explanation

**Preconditions:**
1. Maker has opted into `EcrecoverAuthorizer` — required for any signature-based authorization flow, common in production setups.
2. Maker has at least one active delegate — standard for automated market-making.
3. Maker attempts to revoke the delegate via a signed message rather than a direct `Midnight.setIsAuthorized` call — plausible when the maker is a smart contract or uses a meta-transaction relay.

The front-run requires no special knowledge beyond the delegate's own private key and the current nonce (publicly readable from `nonce[maker]`). The attack is repeatable — every time the maker tries to revoke via `EcrecoverAuthorizer`, the delegate can repeat the nonce-burn. Only one successful front-run is needed to cause permanent damage via `setConsumed`. [6](#0-5) 

## Recommendation

1. **Move nonce increment after signature verification**: Increment `nonce[authorization.authorizer]` only after the signer has been verified, so a failed signature check does not consume the nonce.
2. **Restrict who can sign on behalf of the authorizer**: Consider requiring that only `authorization.authorizer` themselves (not delegates) can sign an `Authorization` struct, since the purpose of EcrecoverAuthorizer is to allow the authorizer to act via signed messages, not to allow delegates to act on their behalf through this peripheral contract.
3. **Separate nonce namespaces**: If delegate signing is intentional, use a separate nonce per `(authorizer, signer)` pair so a delegate cannot burn the authorizer's global nonce.

## Proof of Concept

```solidity
// 1. maker.setIsAuthorized(ecrecoverAuthorizer, true) via Midnight
// 2. maker.setIsAuthorized(delegate, true) via Midnight
// 3. maker signs Authorization{maker, delegate, false, nonce=0, deadline=T+1h} → broadcasts
// 4. delegate signs Authorization{maker, attacker, true, nonce=0, deadline=T+2h}
// 5. delegate calls EcrecoverAuthorizer.setIsAuthorized(step4Auth, step4Sig)
//    → nonce[maker] = 1, isAuthorized[maker][attacker] = true
// 6. maker's tx from step 3 reverts: InvalidNonce (nonce is now 1, not 0)
// 7. attacker calls Midnight.setConsumed(group, type(uint256).max, maker)
//    → consumed[maker][group] = type(uint256).max (irreversible)
// 8. Any take() on maker's offers in `group` now reverts with ConsumedUnits/ConsumedAssets
```

A Foundry fork test can confirm this by: deploying Midnight + EcrecoverAuthorizer, setting up the authorization state, submitting the delegate's front-run transaction first, then confirming the maker's revocation reverts and `setConsumed` succeeds.

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L18-18)
```text
    mapping(address => uint256) public nonce;
```

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

**File:** src/Midnight.sol (L723-728)
```text
    function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        require(amount >= consumed[onBehalf][group], AlreadyConsumed());
        consumed[onBehalf][group] = amount;
        emit EventsLib.SetConsumed(msg.sender, group, amount, onBehalf);
    }
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```

**File:** RESEARCHER.md (L14-14)
```markdown
- Permanent lock, freeze, or unrecoverable corruption of user/project state.
```
