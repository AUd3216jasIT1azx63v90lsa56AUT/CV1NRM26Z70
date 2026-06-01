### Title
Coarse-grained `isAuthorized` delegation allows any authorized operator to un-ratify Merkle roots and freeze all offers - (File: src/ratifiers/SetterRatifier.sol)

### Summary
`SetterRatifier.setIsRootRatified` uses Midnight's global `isAuthorized` mapping as its sole authorization gate, accepting any `(maker, msg.sender)` pair where `isAuthorized[maker][msg.sender] == true`. Because this authorization is coarse-grained and action-agnostic, an operator granted access for any purpose (e.g., collateral management, repayment) can call `setIsRootRatified(maker, root, false)`, setting `isRootRatified[maker][root]` to `false` and causing every subsequent `take` for any offer under that root to revert with `NotRatified`.

### Finding Description
**Exact code path:**

`src/ratifiers/SetterRatifier.sol` lines 24–28:
```solidity
function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
    require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
    isRootRatified[maker][root] = newIsRootRatified;
    emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
}
``` [1](#0-0) 

The check delegates entirely to `IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender)`, which is set via:

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
``` [2](#0-1) 

There is no scope restriction: `isAuthorized` is a single boolean per `(maker, operator)` pair covering every action in the protocol and every peripheral contract that queries it.

**Exploit flow:**
1. Maker calls `midnight.setIsAuthorized(operator, true, maker)` — granting the operator access for any legitimate purpose (e.g., `supplyCollateral`, `repay`).
2. Operator (malicious or later compromised) calls `setterRatifier.setIsRootRatified(maker, root, false)`.
3. `isRootRatified[maker][root]` is now `false`.
4. Any `midnight.take(offer, ...)` where `offer.ratifier == address(setterRatifier)` and the ratifier data encodes `root` hits:

```solidity
require(isRootRatified[offer.maker][root], NotRatified());
``` [3](#0-2) 

and reverts, freezing every offer in the tree.

**Why existing checks fail:** The `Unauthorized()` guard passes because `isAuthorized[maker][operator] == true`. There is no secondary check scoping the operator to only setting `true` vs. `false`, nor any check that the operator was authorized specifically for ratifier management. The `bool newIsRootRatified` parameter is fully attacker-controlled.

### Impact Explanation
All offers whose `ratifier` is this `SetterRatifier` instance and whose Merkle root is `root` become permanently un-takeable until the maker (or another authorized address) re-ratifies the root. If the operator remains authorized, they can immediately re-freeze it. This is a complete, targeted denial-of-service on the maker's entire offer tree: lenders cannot fill borrow offers, borrowers cannot fill lend offers, and no settlement or fee-claim path that depends on a `take` can proceed.

### Likelihood Explanation
**Preconditions:**
- Maker has called `setIsAuthorized(operator, true, maker)` for any reason.
- Maker uses `SetterRatifier` for at least one active offer tree.

Both conditions are normal operational states. The authorization grant is a standard workflow (e.g., a smart-contract vault or keeper authorized to manage collateral). The attack requires a single permissionless call with no capital, no flash loan, and no oracle manipulation. It is repeatable: the operator can re-freeze after every maker re-ratification as long as authorization is not revoked.

### Recommendation
Restrict `setIsRootRatified` so that setting `newIsRootRatified = false` (de-ratification) requires `maker == msg.sender` only, while setting `true` may continue to accept authorized operators:

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

This mirrors the asymmetry already present in `EcrecoverRatifier`, where `cancelRoot` is irreversible and thus implicitly safe from re-activation abuse. [4](#0-3) 

### Proof of Concept
```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {SetterRatifier} from "../src/ratifiers/SetterRatifier.sol";
import {ISetterRatifier} from "../src/ratifiers/interfaces/ISetterRatifier.sol";
import {HashLib} from "../src/ratifiers/libraries/HashLib.sol";
import {BaseTest} from "./BaseTest.sol";
import {CollateralParams, Market, Offer} from "../src/interfaces/IMidnight.sol";
import {MAX_TICK} from "../src/libraries/TickLib.sol";

contract SetterRatifierFreezeTest is BaseTest {
    SetterRatifier internal setterRatifier;

    function setUp() public override {
        super.setUp();
        setterRatifier = new SetterRatifier(address(midnight));
    }

    /// @notice Fuzz: any authorized operator can freeze the maker's root
    function testFuzz_AuthorizedOperatorCanFreezeRoot(address operator) public {
        vm.assume(operator != lender);
        vm.assume(operator != address(0));

        // Build a minimal offer
        Market memory market;
        market.loanToken = address(loanToken);
        market.maturity = vm.getBlockTimestamp() + 100;
        market.collateralParams = new CollateralParams[](1);
        market.collateralParams[0] = CollateralParams({
            token: address(collateralToken1),
            lltv: 0.77e18,
            maxLif: maxLif(0.77e18, 0.25e18),
            oracle: address(oracle1)
        });
        Offer memory offer;
        offer.market = market;
        offer.buy = true;
        offer.maker = lender;
        offer.ratifier = address(setterRatifier);
        offer.maxUnits = type(uint256).max;
        offer.expiry = vm.getBlockTimestamp() + 200;
        offer.tick = MAX_TICK;

        bytes32 root = HashLib.hashOffer(offer);

        // Maker ratifies the root
        vm.prank(lender);
        setterRatifier.setIsRootRatified(lender, root, true);
        assertTrue(setterRatifier.isRootRatified(lender, root));

        // Maker grants authorization to operator for unrelated purposes
        vm.prank(lender);
        midnight.setIsAuthorized(operator, true, lender);

        // Malicious/compromised operator freezes the root
        vm.prank(operator);
        setterRatifier.setIsRootRatified(lender, root, false); // <-- no revert

        // Root is now frozen
        assertFalse(setterRatifier.isRootRatified(lender, root));

        // Any take attempt reverts NotRatified
        vm.prank(address(midnight));
        vm.expectRevert(ISetterRatifier.NotRatified.selector);
        setterRatifier.isRatified(offer, abi.encode(root, 0, new bytes32[](0)));
    }
}
```

**Expected assertions:**
- `setterRatifier.setIsRootRatified(lender, root, false)` does NOT revert when called by `operator` with `isAuthorized[lender][operator] == true`.
- `isRootRatified[lender][root]` transitions from `true` to `false`.
- `isRatified(offer, ...)` reverts with `NotRatified`, confirming the freeze.
- The fuzz covers arbitrary `operator` addresses, confirming any authorized address triggers the bug.

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

**File:** src/Midnight.sol (L731-733)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
```

**File:** src/ratifiers/EcrecoverRatifier.sol (L27-31)
```text
    function cancelRoot(address maker, bytes32 root) external {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootCanceled[maker][root] = true;
        emit CancelRoot(msg.sender, maker, root);
    }
```
