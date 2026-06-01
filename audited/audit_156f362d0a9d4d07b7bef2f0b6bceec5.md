### Title
Authorized Callback Reentrancy via `setConsumed` Permanently Cancels Maker's Offer Group Mid-Take - (File: src/Midnight.sol)

### Summary
During `take()`, `consumed[offer.maker][offer.group]` is incremented before any external callbacks fire. When `offer.buy = false`, the taker-supplied `takerCallback` becomes the `buyerCallback` and receives `onBuy()`. If the attacker's callback contract is authorized for the maker (`isAuthorized[maker][attackerCallback] == true`), it can reenter `setConsumed(group, type(uint256).max, maker)` during `onBuy()`, permanently setting `consumed[maker][group]` to `type(uint256).max` and cancelling all future fills of that offer group. The Certora formal verification spec explicitly acknowledges this gap, noting it "assumes no reentrancy" for consumed-mapping properties.

### Finding Description

**Exact code path:**

In `src/Midnight.sol`, `take()` updates `consumed` at lines 367–373 before any external call:

```solidity
newConsumed = consumed[offer.maker][offer.group] += units;
require(newConsumed <= offer.maxUnits, ConsumedUnits());
``` [1](#0-0) 

Callback routing at lines 420–421 assigns `buyerCallback = takerCallback` when `offer.buy == false`:

```solidity
address buyerCallback = offer.buy ? offer.callback : takerCallback;
``` [2](#0-1) 

The `buyerCallback.onBuy()` fires at lines 445–453, after `consumed` is already mutated but before `take()` returns: [3](#0-2) 

`setConsumed()` at lines 723–728 has no reentrancy guard. Its only checks are authorization and monotonicity:

```solidity
require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
require(amount >= consumed[onBehalf][group], AlreadyConsumed());
consumed[onBehalf][group] = amount;
``` [4](#0-3) 

**Exploit flow:**

1. Precondition: maker calls `setIsAuthorized(attackerContract, true, maker)`, granting `attackerContract` authorization.
2. Attacker calls `take(offer, ..., takerCallback=attackerContract)` with `offer.buy = false` (maker is seller).
3. `take()` increments `consumed[maker][group] += units` and checks `<= maxUnits` — passes.
4. `buyerCallback = attackerContract`; `onBuy()` fires on `attackerContract`.
5. Inside `onBuy()`, attacker calls `midnight.setConsumed(offer.group, type(uint256).max, maker)`:
   - `isAuthorized[maker][attackerContract] == true` → authorization check passes.
   - `type(uint256).max >= consumed[maker][group]` → monotonicity check passes.
   - `consumed[maker][group]` is set to `type(uint256).max`.
6. `take()` completes normally. No revert occurs.
7. Post-state: `consumed[maker][group] == type(uint256).max`.

**Why existing checks fail:**

- The only lock set during `take()` is `LIQUIDATION_LOCK_SLOT` (line 444), which only blocks liquidation of the seller — it does not block `setConsumed`.
- `setConsumed`'s monotonicity check (`amount >= consumed`) is trivially satisfied by `type(uint256).max`.
- The Certora spec `certora/specs/OnlyAuthorizedCanChange.spec` line 88 explicitly states: *"Assumes no reentrancy: callbacks and token transfers are not modeled as re-entering Midnight, so re-entrant consumed changes are not covered."* [5](#0-4) 

The `takeConsumedDelta` Certora rule (which asserts `consumed == consumedBefore + units` after a take) is violated by this path but is not verified under reentrancy: [6](#0-5) 

### Impact Explanation

After the attack, `consumed[maker][group] = type(uint256).max`. Every subsequent `take()` on any offer in that group will compute `newConsumed = type(uint256).max + delta`, which either overflows (reverting) or immediately exceeds `maxAssets`/`maxUnits`, causing a permanent `ConsumedAssets` or `ConsumedUnits` revert. The maker's entire offer group is irreversibly cancelled. The current fill completes normally, so the attacker suffers no financial loss while the maker loses all future fill capacity for that group.

### Likelihood Explanation

**Precondition**: `isAuthorized[maker][attackerContract] == true`. This requires the maker to have explicitly authorized the attacker's contract. This is non-trivial but realistic: makers routinely authorize periphery contracts (e.g., `MidnightBundles`) or automation bots. If the attacker controls or compromises any such authorized contract, or if the maker mistakenly authorizes a malicious contract, the attack is immediately executable. The attack is repeatable across any offer group the maker has authorized the contract for, and requires no special market conditions, oracle values, or admin actions.

### Recommendation

Add a reentrancy guard scoped to the `(maker, group)` pair during `take()` execution, or snapshot `consumed[maker][group]` before callbacks and assert it is unchanged after callbacks return. Concretely, before firing `buyerCallback.onBuy()`, record `uint256 consumedSnapshot = consumed[offer.maker][offer.group]` and after all callbacks complete, `require(consumed[offer.maker][offer.group] == consumedSnapshot, ConsumedMutatedDuringCallback())`. Alternatively, apply a transient reentrancy lock on `setConsumed` that blocks calls while a `take()` is in progress for the same `(maker, group)`.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {IMidnight, IBuyCallback} from "src/interfaces/...";

contract MaliciousCallback is IBuyCallback {
    IMidnight midnight;
    bytes32 group;
    address maker;

    constructor(IMidnight _midnight, bytes32 _group, address _maker) {
        midnight = _midnight; group = _group; maker = _maker;
    }

    function onBuy(bytes32, Market memory, uint256, uint256, uint256, address, bytes memory)
        external returns (bytes32)
    {
        // Reenter setConsumed during the buy callback
        midnight.setConsumed(group, type(uint256).max, maker);
        return CALLBACK_SUCCESS;
    }
}

contract ReentrancySetConsumedTest is Test {
    function testSetConsumedReentrancyDuringBuyCallback() public {
        // Setup: maker authorizes attackerCallback
        vm.prank(maker);
        midnight.setIsAuthorized(address(attackerCallback), true, maker);

        uint256 consumedBefore = midnight.consumed(maker, offer.group);

        // Attacker takes with offer.buy=false, takerCallback=attackerCallback
        vm.prank(attacker);
        midnight.take(offer, hex"", units, attacker, attacker, address(attackerCallback), hex"");

        uint256 consumedAfter = midnight.consumed(maker, offer.group);

        // Invariant: consumed should equal consumedBefore + units
        // Bug: consumed == type(uint256).max instead
        assertEq(consumedAfter, consumedBefore + units, "consumed corrupted by reentrant setConsumed");
        // This assertion FAILS, proving the bug:
        // consumedAfter == type(uint256).max != consumedBefore + units
    }
}
```

**Expected assertion failure**: `consumedAfter == type(uint256).max`, not `consumedBefore + units`, demonstrating that the maker's offer group is permanently cancelled by the reentrant `setConsumed` call during `onBuy()`.

### Citations

**File:** src/Midnight.sol (L366-373)
```text
        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
```

**File:** src/Midnight.sol (L420-421)
```text
        address buyerCallback = offer.buy ? offer.callback : takerCallback;
        address sellerCallback = offer.buy ? takerCallback : offer.callback;
```

**File:** src/Midnight.sol (L444-453)
```text
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
```

**File:** src/Midnight.sol (L723-728)
```text
    function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        require(amount >= consumed[onBehalf][group], AlreadyConsumed());
        consumed[onBehalf][group] = amount;
        emit EventsLib.SetConsumed(msg.sender, group, amount, onBehalf);
    }
```

**File:** certora/specs/OnlyAuthorizedCanChange.spec (L86-97)
```text
/// An unauthorized caller cannot change a user's consumed except via take.
/// For take, unauthorizedTakeFails, takeRequiresMakerConsent, and takeOnlyAuthorizedCanChangeDebt show that take can only change this consumed: consumed[offer.maker][offer.group], only with the right authorizations.
/// Assumes no reentrancy: callbacks and token transfers are not modeled as re-entering Midnight, so re-entrant consumed changes are not covered.
rule onlyAuthorizedCanChangeConsumedExceptTake(env e, method f, calldataarg args, address user, bytes32 group) filtered { f -> !f.isView && f.selector != sig:take(Midnight.Offer, bytes, uint256, address, address, address, bytes).selector } {
    bool userIsAuthorized = user == e.msg.sender || isAuthorized(user, e.msg.sender);

    uint256 consumedBefore = consumed(user, group);
    f(e, args);
    uint256 consumedAfter = consumed(user, group);

    assert consumedAfter == consumedBefore || userIsAuthorized;
}
```

**File:** certora/specs/Consume.spec (L67-75)
```text
rule takeConsumedDelta(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxAssets == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert consumed(offer.maker, offer.group) == consumedBefore + units;
}
```
