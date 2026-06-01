### Title
Blanket `isAuthorized` check in `setIsRootRatified` allows any authorized operator to de-ratify maker roots, blocking `take()` - (`src/ratifiers/SetterRatifier.sol`)

### Summary
`SetterRatifier.setIsRootRatified` guards access with Midnight's blanket `isAuthorized(maker, msg.sender)` mapping, which carries no action scope. Any address the maker has authorized for any purpose — including `repay()` — can call `setIsRootRatified(maker, root, false)`, flipping `isRootRatified[maker][root]` to `false` and causing every subsequent `take()` against that maker's offers to revert with `NotRatified()`.

### Finding Description

**Root cause — no action scoping on a destructive write:**

`setIsRootRatified` at [1](#0-0)  performs a single boolean gate:

```solidity
require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
isRootRatified[maker][root] = newIsRootRatified;
```

`IMidnight.isAuthorized` is a flat `mapping(address => mapping(address => bool))` with no concept of which action the authorization was granted for. [2](#0-1) 

`isRatified` enforces that `isRootRatified[offer.maker][root]` must be `true` or it reverts: [3](#0-2) 

**Exploit flow:**

1. Maker calls `midnight.setIsAuthorized(attacker, true, maker)` — granting attacker permission to call `repay()` on maker's behalf (or any other Midnight action).
2. Maker calls `setterRatifier.setIsRootRatified(maker, root, true)` — activating their offer tree.
3. Attacker calls `setterRatifier.setIsRootRatified(maker, root, false)` — the `isAuthorized` check passes because `isAuthorized[maker][attacker] == true`.
4. `isRootRatified[maker][root]` is now `false`.
5. Every `midnight.take(offer, ...)` where `offer.ratifier == address(setterRatifier)` and `offer.maker == maker` reverts with `NotRatified()`.

**Why existing checks do not stop it:**

The only guard is the `isAuthorized` check at line 25. There is no separate check that the caller was authorized specifically for ratifier management, no `onlyMaker` path for the destructive (`false`) direction, and no cooldown or time-lock. The attacker can repeat the call immediately after the maker re-ratifies, making the DoS persistent at negligible cost (one `SSTORE` per iteration).

**Contrast with `EcrecoverRatifier.cancelRoot`:** that function only ever sets `isRootCanceled[maker][root] = true` — it is unidirectional and therefore cannot be weaponized to un-cancel. [4](#0-3)  `SetterRatifier.setIsRootRatified` is bidirectional, which is what creates the attack surface.

### Impact Explanation

Any maker using `SetterRatifier` who has ever granted `isAuthorized` to any counterparty (e.g., a repay operator, a withdrawal helper, a multicall bundler) is permanently exposed. The attacker can block all `take()` calls against that maker's offers with a single low-gas transaction, repeated indefinitely. This is a cross-action DoS: authorization granted for one Midnight action (repay) silently confers the power to destroy the maker's entire lending/borrowing offer activity.

### Likelihood Explanation

- **Precondition:** `isAuthorized[maker][attacker] == true` for any reason. This is a normal operational state — makers routinely authorize operators for repay, withdraw, or collateral management.
- **Attacker cost:** one external call to `setIsRootRatified` with `false`; no tokens, no collateral, no flash loan required.
- **Repeatability:** unlimited; the maker cannot prevent it without revoking all authorizations, which breaks their other delegated operations.
- **Discovery:** the attacker only needs to observe on-chain `setIsAuthorized` events to identify targets.

### Recommendation

Restrict the de-ratification direction (`newIsRootRatified == false`) to `maker == msg.sender` only. Authorized operators should be permitted to ratify (activate) roots on the maker's behalf but not to revoke them:

```solidity
function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
    if (!newIsRootRatified) {
        require(maker == msg.sender, Unauthorized());
    } else {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
    }
    isRootRatified[maker][root] = newIsRootRatified;
    emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
}
```

This mirrors the unidirectional design of `EcrecoverRatifier.cancelRoot` and ensures that de-ratification is a maker-only destructive action.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {Test} from "forge-std/Test.sol";
import {SetterRatifier} from "src/ratifiers/SetterRatifier.sol";
import {ISetterRatifier} from "src/ratifiers/interfaces/ISetterRatifier.sol";
import {HashLib} from "src/ratifiers/libraries/HashLib.sol";
import {Offer} from "src/interfaces/IMidnight.sol";
// ... BaseTest setup

contract SetterRatifierCrossActionDoSTest is BaseTest {
    SetterRatifier setterRatifier;

    function setUp() public override {
        super.setUp();
        setterRatifier = new SetterRatifier(address(midnight));
    }

    function testRepayAuthorizedOperatorCanDeRatifyMakerRoot() public {
        address maker   = lender;
        address attacker = borrower;

        // Step 1: maker ratifies their offer root
        Offer memory offer = makeOffer(maker);
        bytes32 root = HashLib.hashOffer(offer);
        vm.prank(maker);
        setterRatifier.setIsRootRatified(maker, root, true);
        assertTrue(setterRatifier.isRootRatified(maker, root));

        // Step 2: maker authorizes attacker only for repay (blanket isAuthorized)
        vm.prank(maker);
        midnight.setIsAuthorized(attacker, true, maker);

        // Step 3: attacker de-ratifies the root — should revert but does NOT
        vm.prank(attacker);
        setterRatifier.setIsRootRatified(maker, root, false);

        // Step 4: root is now de-ratified
        assertFalse(setterRatifier.isRootRatified(maker, root));

        // Step 5: take() now reverts with NotRatified
        vm.prank(address(midnight));
        vm.expectRevert(ISetterRatifier.NotRatified.selector);
        setterRatifier.isRatified(offer, abi.encode(root, 0, new bytes32[](0)));
    }
}
```

**Expected assertions:**
- `setterRatifier.isRootRatified(maker, root)` is `false` after attacker's call — confirms de-ratification succeeded.
- `isRatified(offer, ...)` reverts with `NotRatified()` — confirms `take()` is blocked.
- No privileged access was used; attacker only held a standard `isAuthorized` grant.

### Citations

**File:** src/ratifiers/SetterRatifier.sol (L24-28)
```text
    function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootRatified[maker][root] = newIsRootRatified;
        emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
    }
```

**File:** src/ratifiers/SetterRatifier.sol (L35-35)
```text
        require(isRootRatified[offer.maker][root], NotRatified());
```

**File:** src/interfaces/IMidnight.sol (L123-123)
```text
    function isAuthorized(address authorizer, address authorized) external view returns (bool);
```

**File:** src/ratifiers/EcrecoverRatifier.sol (L27-31)
```text
    function cancelRoot(address maker, bytes32 root) external {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootCanceled[maker][root] = true;
        emit CancelRoot(msg.sender, maker, root);
    }
```
