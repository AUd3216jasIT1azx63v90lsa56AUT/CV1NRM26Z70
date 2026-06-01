### Title
Authorized operator can silently disable maker's Merkle-root offers via `SetterRatifier.setIsRootRatified` - (File: src/ratifiers/SetterRatifier.sol)

### Summary
`SetterRatifier.setIsRootRatified` reuses `Midnight.isAuthorized` as its sole access-control gate. Any address that a maker has authorized in `Midnight` — for any purpose, including acting as a taker — can call `setIsRootRatified(maker, root, false)` and atomically disable every offer the maker has published under that Merkle root. The maker's live offers become permanently unfillable until they notice and re-enable the root, with no on-chain signal beyond an event.

### Finding Description
**Code path:**

`SetterRatifier.setIsRootRatified` (line 24–27): [1](#0-0) 

```solidity
function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
    require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
    isRootRatified[maker][root] = newIsRootRatified;
```

The authorization check delegates entirely to `Midnight.isAuthorized[maker][msg.sender]`. [2](#0-1) 

`Midnight.take` enforces ratification at lines 355–356: [3](#0-2) 

```solidity
require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

`SetterRatifier.isRatified` enforces the root flag at line 35: [4](#0-3) 

```solidity
require(isRootRatified[offer.maker][root], NotRatified());
```

**Exploit flow:**

1. Maker calls `Midnight.setIsAuthorized(operator, true, maker)` — e.g., to let `operator` act as a taker on their behalf.
2. Maker calls `SetterRatifier.setIsRootRatified(maker, root, true)` to publish a batch of offers under `root`.
3. Attacker (the authorized `operator`) calls `SetterRatifier.setIsRootRatified(maker, root, false)`.
   - Auth check: `IMidnight(MIDNIGHT).isAuthorized(maker, operator)` → `true` → passes.
   - `isRootRatified[maker][root]` is set to `false`.
4. Any subsequent `Midnight.take` on any offer in that root calls `isRatified`, which hits `require(isRootRatified[offer.maker][root], NotRatified())` and reverts.

**Why existing checks fail:**

`setIsRootRatified` has no scope restriction — it accepts any `isAuthorized` entry regardless of why the authorization was granted. `Midnight.take` checks `isAuthorized[offer.maker][offer.ratifier]` (line 355) to confirm the maker trusts the ratifier contract, but this is a separate check from who may mutate the ratifier's state. There is no check in `take` or `SetterRatifier` that prevents an authorized-for-taking operator from writing to `isRootRatified`. [5](#0-4) 

### Impact Explanation
All of the maker's live offers under the targeted root become immediately unfillable. If the maker is a lender with resting sell offers, they lose expected yield for the duration of the disruption. If the maker is a borrower with resting buy offers, they lose borrowing capacity. The attack is silent (only an event is emitted), repeatable (the attacker can re-disable the root every time the maker re-enables it), and costs only gas.

### Likelihood Explanation
Preconditions: (1) maker has authorized any address in `Midnight` — a routine action for delegation, automation, or smart-contract integrations; (2) that address is controlled by or accessible to an adversary. The authorization need not be for ratifier management; any `isAuthorized` entry suffices. The attack is a single external call with no token cost and is trivially repeatable.

### Recommendation
`SetterRatifier` should maintain its own independent authorization mapping rather than delegating to `Midnight.isAuthorized`. For example:

```solidity
mapping(address maker => mapping(address operator => bool)) public isSetterAuthorized;

function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
    require(maker == msg.sender || isSetterAuthorized[maker][msg.sender], Unauthorized());
    ...
}
```

This decouples Midnight-level delegation (taker, repayer, collateral manager) from the ability to mutate ratification state, enforcing least-privilege. Alternatively, if reuse of `Midnight.isAuthorized` is intentional, the `Midnight` documentation and `SetterRatifier` interface must prominently warn that any authorized address gains full control over the maker's ratified roots, and users should only authorize scoped smart contracts — never EOAs or untrusted addresses.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {SetterRatifier} from "src/ratifiers/SetterRatifier.sol";

contract OperatorDisablesRootTest is Test {
    Midnight midnight;
    SetterRatifier ratifier;
    address maker  = address(0x1);
    address operator = address(0x2); // authorized for taking, not for ratifier mgmt
    bytes32 root = keccak256("merkle-root");

    function setUp() public {
        midnight = new Midnight();
        ratifier = new SetterRatifier(address(midnight));

        // maker authorizes operator (e.g., for taking on their behalf)
        vm.prank(maker);
        midnight.setIsAuthorized(operator, true, maker);

        // maker enables their offer root
        vm.prank(maker);
        ratifier.setIsRootRatified(maker, root, true);
    }

    function test_operatorCanDisableMakerRoot() public {
        // precondition: root is ratified
        assertTrue(ratifier.isRootRatified(maker, root));

        // operator disables the root — no special privilege needed beyond isAuthorized
        vm.prank(operator);
        ratifier.setIsRootRatified(maker, root, false);

        // assert: root is now disabled
        assertFalse(ratifier.isRootRatified(maker, root));

        // assert: any subsequent take on maker's offers in this root will revert with NotRatified
        // (wire up a valid Offer with offer.ratifier = address(ratifier) and call midnight.take;
        //  expect revert SetterRatifier.NotRatified)
    }
}
```

**Expected assertions:**
- `ratifier.isRootRatified(maker, root)` is `false` after the operator's call.
- `midnight.take(offer, ratifierData, ...)` reverts with `SetterRatifier.NotRatified` for any offer in that root.
- The maker can re-enable the root, but the operator can immediately disable it again in the same block.

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

**File:** src/Midnight.sol (L192-192)
```text
    mapping(address authorizer => mapping(address authorized => bool)) public isAuthorized;
```

**File:** src/Midnight.sol (L346-346)
```text
        require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
```

**File:** src/Midnight.sol (L355-356)
```text
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```
