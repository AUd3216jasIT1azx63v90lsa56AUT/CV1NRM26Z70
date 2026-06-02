Audit Report

## Title
Co-authorized agent can deauthorize MidnightBundles via EcrecoverAuthorizer, causing permanent DoS on all bundled operations - (File: src/periphery/EcrecoverAuthorizer.sol)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts a signature from any address that is already authorized by `authorization.authorizer`, not just the authorizer themselves. This allows a co-authorized agent to sign a new `Authorization` struct revoking `MidnightBundles` from the victim's authorization mappings. All five `MidnightBundles` entry points then revert with `Unauthorized` for that victim until they re-authorize — which can be griefed again immediately at zero capital cost.

## Finding Description

**Root cause — `EcrecoverAuthorizer.setIsAuthorized` lines 33–36:**

```solidity
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
``` [1](#0-0) 

The second branch of the `||` grants any currently-authorized agent of `authorization.authorizer` the ability to sign a *new* `Authorization` struct — not merely relay a pre-signed one from the authorizer. There is no restriction on which `authorization.authorized` address may be targeted or what `authorization.isAuthorized` value may be set.

**Exploit flow:**

1. Victim calls `Midnight.setIsAuthorized(MidnightBundles, true, victim)` and `Midnight.setIsAuthorized(attacker, true, victim)`.
2. Attacker constructs `Authorization(authorizer=victim, authorized=MidnightBundles, isAuthorized=false, nonce=currentNonce[victim], deadline=future)` and signs it with the attacker's own private key.
3. Attacker calls `EcrecoverAuthorizer.setIsAuthorized(authorization, attackerSig)`.
4. `ecrecover` returns `attacker`; `IMidnight(MIDNIGHT).isAuthorized(victim, attacker) == true` passes.
5. Line 46–47 executes `IMidnight(MIDNIGHT).setIsAuthorized(MidnightBundles, false, victim)`. [2](#0-1) 

**Why existing checks fail:**

- The nonce check (line 26) only prevents replay of the same struct; it does not restrict which `authorized` address or `isAuthorized` value the signer may choose. [3](#0-2) 
- The deadline check (line 25) is irrelevant to authorization scope.
- There is no guard preventing an authorized agent from targeting a *different* authorized agent for revocation.

**Downstream revert in MidnightBundles:**

All five entry points gate on:
```solidity
require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
``` [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) 

After step 5, `isAuthorized(victim, MidnightBundles) == false`, so every call with `taker=victim` reverts.

## Impact Explanation
Permanent, repeatable DoS on `buyWithUnitsTargetAndWithdrawCollateral`, `buyWithAssetsTargetAndWithdrawCollateral`, `supplyCollateralAndSellWithUnitsTarget`, `supplyCollateralAndSellWithAssetsTarget`, and `repayAndWithdrawCollateral` for any victim who has authorized both `MidnightBundles` and at least one other address. This constitutes a "permanent lock/freeze of user state" as defined in RESEARCHER.md. No funds are directly stolen, but the victim is blocked from using the bundler's core functionality indefinitely.

## Likelihood Explanation
- **Precondition:** Victim has authorized both `MidnightBundles` and at least one other address (e.g., a keeper, UI relayer, or second wallet). This is the normal operating state for any active bundler user.
- **Attacker cost:** Gas for one `setIsAuthorized` call. No capital required.
- **Repeatability:** Every time the victim re-authorizes `MidnightBundles`, the attacker can immediately deauthorize it again, making the DoS indefinitely sustainable.
- **Detection:** `SetIsAuthorized` is emitted but there is no on-chain protection against it. [9](#0-8) 

## Recommendation
Restrict the signer authority in `EcrecoverAuthorizer.setIsAuthorized` so that only the authorizer themselves (i.e., `signer == authorization.authorizer`) may sign authorization changes. Remove the second branch `|| IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer)` entirely, or — if delegation is intentional — add a constraint that the signer may only modify their *own* entry (i.e., `authorization.authorized == signer`), preventing cross-agent revocation.

## Proof of Concept
1. Deploy `Midnight` and `EcrecoverAuthorizer` on a local fork.
2. As `victim`, call `Midnight.setIsAuthorized(MidnightBundles, true, victim)` and `Midnight.setIsAuthorized(attacker, true, victim)`.
3. As `attacker`, sign `Authorization(authorizer=victim, authorized=MidnightBundles, isAuthorized=false, nonce=0, deadline=block.timestamp+1000)` with the attacker's private key.
4. Call `EcrecoverAuthorizer.setIsAuthorized(authorization, attackerSig)` — expect success.
5. Assert `IMidnight(MIDNIGHT).isAuthorized(victim, MidnightBundles) == false`.
6. Attempt any `MidnightBundles` entry point with `taker=victim` — expect revert with `Unauthorized`.
7. As `victim`, re-authorize `MidnightBundles`; repeat steps 3–6 to confirm indefinite repeatability.

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

**File:** src/periphery/EcrecoverAuthorizer.sol (L38-44)
```text
        emit SetIsAuthorized(
            msg.sender,
            authorization.authorizer,
            authorization.authorized,
            authorization.isAuthorized,
            authorization.nonce
        );
```

**File:** src/periphery/EcrecoverAuthorizer.sol (L46-47)
```text
        IMidnight(MIDNIGHT)
            .setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
```

**File:** src/periphery/MidnightBundles.sol (L60-60)
```text
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
```

**File:** src/periphery/MidnightBundles.sol (L127-127)
```text
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
```

**File:** src/periphery/MidnightBundles.sol (L191-191)
```text
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
```

**File:** src/periphery/MidnightBundles.sol (L262-262)
```text
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
```

**File:** src/periphery/MidnightBundles.sol (L325-325)
```text
        require(onBehalf == msg.sender || IMidnight(MIDNIGHT).isAuthorized(onBehalf, msg.sender), Unauthorized());
```
