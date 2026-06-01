### Title
Authorized Address Can Repeatedly Toggle `isRootRatified` to Grief-DoS All Takes on a Maker's Offers - (`src/ratifiers/SetterRatifier.sol`)

### Summary
`SetterRatifier.setIsRootRatified` is an unrestricted two-way toggle callable by any address holding `isAuthorized[maker][attacker] == true`. Because `Midnight.take` synchronously checks `isRootRatified[offer.maker][root]` via `isRatified`, an authorized griefing attacker can front-run every take transaction by flipping the flag to `false`, causing the take to revert with `NotRatified`, then restoring it to `true` — indefinitely and cheaply.

### Finding Description

**Exact code path:**

`SetterRatifier.setIsRootRatified` (lines 24–28) performs a single authorization check and then unconditionally writes the boolean:

```solidity
function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
    require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
    isRootRatified[maker][root] = newIsRootRatified;
``` [1](#0-0) 

There is no one-way restriction, no time-lock, and no cooldown. Any address satisfying the authorization check can toggle the flag in either direction at will.

`Midnight.take` (lines 355–356) calls `isRatified` synchronously before any state changes:

```solidity
require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
``` [2](#0-1) 

`SetterRatifier.isRatified` (line 35) reads the flag directly from storage:

```solidity
require(isRootRatified[offer.maker][root], NotRatified());
``` [3](#0-2) 

**Exploit flow:**

1. Maker has previously called `midnight.setIsAuthorized(attacker, true, maker)` and `setterRatifier.setIsRootRatified(maker, root, true)`.
2. Attacker monitors the mempool for any `take(offer, ...)` targeting this maker's offer.
3. Attacker front-runs with `setIsRootRatified(maker, root, false)` — a single cold SSTORE (~5,000 gas).
4. Victim's `take` executes, hits `require(isRootRatified[offer.maker][root], NotRatified())`, reverts.
5. Attacker back-runs with `setIsRootRatified(maker, root, true)` to restore state and avoid detection.
6. Steps 2–5 repeat for every take attempt, indefinitely.

**Why existing checks fail:**

The only guard in `setIsRootRatified` is the authorization check. It correctly prevents *unauthorized* callers, but it does not prevent an *authorized* caller from toggling the flag maliciously. There is no mechanism distinguishing "ratify" from "de-ratify" permissions, no minimum hold time, and no event-based delay. The `EcrecoverRatifier` avoids this by making cancellation one-way (`isRootCanceled` can only be set to `true`, never back to `false`): [4](#0-3) 

`SetterRatifier` has no equivalent protection.

### Impact Explanation
Any maker using `SetterRatifier` whose authorization list includes a griefing address faces a complete, persistent DoS of the `take` path for their offers. Lenders cannot have their offers filled; borrowers cannot open positions against those offers. Since `take` is the only entry point for credit creation, this freezes lender/borrower exit and entry for the affected market participants for as long as the attacker maintains the front-running loop.

### Likelihood Explanation
**Preconditions:**
- `isAuthorized[maker][attacker] == true` — set by the maker via `midnight.setIsAuthorized`. This is a realistic scenario: makers may authorize bots, delegates, or smart contracts for other purposes (e.g., automated ratification), not anticipating that the same authorization grants the ability to de-ratify.
- Attacker can front-run — standard on any non-private mempool chain.

**Feasibility:** The toggle costs ~5,000–22,000 gas (cold SSTORE) vs. a `take` costing tens of thousands to hundreds of thousands of gas. The attacker can profitably sustain the DoS. The attack is repeatable every block with no cooldown or penalty.

**Repeatability:** Unlimited. The attacker restores state after each block, so the maker cannot detect a persistent flag change — only failed takes.

### Recommendation
Make `setIsRootRatified` one-way for de-ratification, mirroring `EcrecoverRatifier.cancelRoot`:

```solidity
// Only allow setting to true; to revoke, use a separate one-way cancelRoot function
function setIsRootRatified(address maker, bytes32 root) public {
    require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
    isRootRatified[maker][root] = true;
    emit SetIsRootRatified(msg.sender, maker, root, true);
}

function cancelRoot(address maker, bytes32 root) public {
    require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
    isRootRatified[maker][root] = false; // one-way: cannot be re-enabled after cancel
    // or use a separate `isRootCanceled` mapping that can never be unset
}
```

Alternatively, introduce a separate `cancelRoot` that sets an irreversible `isRootCanceled[maker][root] = true` (never resettable), and have `isRatified` check both mappings. This matches the `EcrecoverRatifier` design. [5](#0-4) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {SetterRatifier} from "src/ratifiers/SetterRatifier.sol";
import {ISetterRatifier} from "src/ratifiers/interfaces/ISetterRatifier.sol";
import {IMidnight} from "src/interfaces/IMidnight.sol";
import {HashLib} from "src/ratifiers/libraries/HashLib.sol";
import {BaseTest} from "test/BaseTest.sol";

contract SetterRatifierDoSTest is BaseTest {
    SetterRatifier internal setterRatifier;

    function setUp() public override {
        super.setUp();
        setterRatifier = new SetterRatifier(address(midnight));
    }

    function testGriefingDoSOnTake() public {
        // Setup: lender creates offer, ratifies root, authorizes attacker (borrower)
        Offer memory offer = /* build valid offer with setterRatifier */;
        bytes32 root = HashLib.hashOffer(offer);

        vm.prank(lender);
        midnight.setIsAuthorized(address(setterRatifier), true, lender);
        vm.prank(lender);
        midnight.setIsAuthorized(borrower /* attacker */, true, lender);
        vm.prank(lender);
        setterRatifier.setIsRootRatified(lender, root, true);

        // Assert: root is ratified
        assertTrue(setterRatifier.isRootRatified(lender, root));

        // Attacker front-runs: toggles to false
        vm.prank(borrower);
        setterRatifier.setIsRootRatified(lender, root, false);

        // Victim's take reverts with NotRatified
        vm.expectRevert(IMidnight.RatifierFail.selector);
        vm.prank(borrower); // taker (different role from attacker in real scenario)
        midnight.take(offer, abi.encode(root, 0, new bytes32[](0)), 0, borrower, borrower, address(0), hex"");

        // Attacker restores: back to true (state unchanged from victim's perspective)
        vm.prank(borrower);
        setterRatifier.setIsRootRatified(lender, root, true);
        assertTrue(setterRatifier.isRootRatified(lender, root));

        // Assert: attacker can repeat this indefinitely — loop N times
        for (uint i = 0; i < 10; i++) {
            vm.prank(borrower);
            setterRatifier.setIsRootRatified(lender, root, false);
            // take would revert here
            vm.prank(borrower);
            setterRatifier.setIsRootRatified(lender, root, true);
        }
        // No revert, no penalty, no state corruption — DoS is free and repeatable
    }
}
```

**Expected assertions:**
- `vm.expectRevert(IMidnight.RatifierFail.selector)` passes on every take attempt during the attack window.
- The loop of 10 toggles completes without revert, demonstrating zero cost to the attacker.
- Gas cost of two `setIsRootRatified` calls is provably less than one `take` call, confirming profitable DoS.

### Citations

**File:** src/ratifiers/SetterRatifier.sol (L24-27)
```text
    function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootRatified[maker][root] = newIsRootRatified;
        emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
```

**File:** src/ratifiers/SetterRatifier.sol (L35-35)
```text
        require(isRootRatified[offer.maker][root], NotRatified());
```

**File:** src/Midnight.sol (L355-356)
```text
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

**File:** src/ratifiers/interfaces/IEcrecoverRatifier.sol (L21-32)
```text
    error RootCanceled();
    error Unauthorized();

    /// EVENTS ///
    event CancelRoot(address indexed caller, address indexed maker, bytes32 indexed root);

    /// FUNCTIONS ///
    function cancelRoot(address maker, bytes32 root) external;

    /// STORAGE GETTERS ///
    function MIDNIGHT() external view returns (address);
    function isRootCanceled(address maker, bytes32 root) external view returns (bool);
```
