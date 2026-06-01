### Title
Maker callback can call `setConsumed` mid-`take` to inflate consumed and grief taker multicall fills - (File: src/Midnight.sol)

### Summary
In `take`, `consumed[offer.maker][offer.group]` is incremented and bounds-checked before any external callback is invoked. For a buy offer, `offer.callback` is the maker's own callback (`onBuy`). Because `setConsumed` has no guard against being called during a `take`, a maker-controlled callback can re-enter `setConsumed` to inflate `consumed` beyond the amount actually filled, permanently blocking subsequent fills of the same offer within the same transaction.

### Finding Description
**Code path:**

1. `take` increments `consumed` and checks the cap before any callback: [1](#0-0) 

2. For `offer.buy == true`, `buyerCallback = offer.callback` — the maker's own address: [2](#0-1) 

3. The maker's `onBuy` callback is invoked with no reentrancy guard on `setConsumed`: [3](#0-2) 

4. `setConsumed` only requires `amount >= consumed[onBehalf][group]` and that `msg.sender` is authorized for `onBehalf`: [4](#0-3) 

**Attacker-controlled inputs:**
- `offer.callback` = malicious contract `M` (maker-deployed)
- `isAuthorized[maker][M] = true` (set by maker via `setIsAuthorized`)
- `offer.maxAssets = MAX` (e.g. 1000)

**Exploit flow:**
1. Maker deploys `M` and calls `setIsAuthorized(M, true, maker)`.
2. Maker publishes buy offer with `offer.callback = M`, `offer.maxAssets = 1000`.
3. Taker submits `multicall([take(offer, 400 units), take(offer, 400 units)])`.
4. First `take`: `consumed[maker][group] += 400` → 400 ≤ 1000, passes.
5. `M.onBuy(...)` is called; inside, `M` calls `midnight.setConsumed(group, 1000, maker)`. Authorization check passes (`isAuthorized[maker][M]`). `AlreadyConsumed` check passes (1000 ≥ 400). `consumed[maker][group]` is now 1000.
6. First `take` completes.
7. Second `take`: `consumed[maker][group] += 400` → 1400 > 1000 → reverts `ConsumedAssets`.
8. Entire multicall reverts; taker's atomic bundle fails.

**Why existing checks fail:**
- `AlreadyConsumed` only prevents *decreasing* consumed; it explicitly allows any increase, so the inflation call passes.
- There is no transient lock or reentrancy guard on `setConsumed` during `take`.
- The Certora `Consume.spec` rules (`takeConsumedBoundedByMax`, `takeConsumedDelta`) model callbacks as `HAVOC_ALL`, so they do not catch this cross-function reentrancy. [5](#0-4) 

### Impact Explanation
A maker can grief any taker who attempts multiple partial fills of the same offer atomically (via `multicall` or a bundler contract). The maker's callback inflates `consumed` to `maxAssets` after the first fill, causing every subsequent fill in the same transaction to revert with `ConsumedAssets`. The entire atomic bundle reverts, wasting the taker's gas and preventing the intended fill. The maker's offer is also permanently blocked from further fills (self-harm griefing).

### Likelihood Explanation
- **Preconditions:** Maker must deploy a callback contract and pre-authorize it. Both are normal, permissionless operations already used in the protocol (e.g., `BorrowCallback`, `LendCallback` patterns in tests).
- **Feasibility:** Trivial — no special privileges, no oracle manipulation, no token owner action required.
- **Repeatability:** The maker can deploy a fresh callback and offer for each target taker. The attack is deterministic and always succeeds given the preconditions.
- **Victim surface:** Any taker using `multicall` or a bundler (e.g., `MidnightBundles`) for multi-fill atomic transactions.

### Recommendation
Prevent `setConsumed` from being called during an active `take` for the same `(maker, group)` pair. The cleanest fix is to use a transient-storage lock (analogous to `LIQUIDATION_LOCK_SLOT`) set around the consumed-accounting block in `take` and checked at the top of `setConsumed`. Alternatively, snapshot `consumed` before the callback and assert it is unchanged after, reverting if the callback modified it.

### Proof of Concept
```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {Test} from "forge-std/Test.sol";
import {Midnight, Offer} from "src/Midnight.sol";
import {IBuyCallback} from "src/interfaces/ICallbacks.sol";
import {CALLBACK_SUCCESS} from "src/interfaces/IMidnight.sol";

contract MaliciousCallback is IBuyCallback {
    Midnight public midnight;
    bytes32 public group;
    address public maker;
    uint256 public inflatedAmount;

    constructor(Midnight _midnight, bytes32 _group, address _maker, uint256 _inflated) {
        midnight = _midnight; group = _group; maker = _maker; inflatedAmount = _inflated;
    }

    function onBuy(bytes32, Market memory, uint256, uint256, uint256, address, bytes memory)
        external returns (bytes32)
    {
        // Inflate consumed to maxAssets, blocking any subsequent fill
        midnight.setConsumed(group, inflatedAmount, maker);
        return CALLBACK_SUCCESS;
    }
}

contract ConsumedInflationTest is Test {
    function testMakerCallbackInflatesConsumedBlockingMulticall() public {
        // Setup: deploy Midnight, create market, fund maker/taker
        // ...

        uint256 maxAssets = 1000e18;
        bytes32 group = keccak256("group1");

        MaliciousCallback cb = new MaliciousCallback(midnight, group, maker, maxAssets);

        vm.prank(maker);
        midnight.setIsAuthorized(address(cb), true, maker);

        Offer memory offer = Offer({
            buy: true,
            maker: maker,
            callback: address(cb),
            maxAssets: maxAssets,
            // ... other fields
        });

        // Taker attempts two partial fills atomically via multicall
        bytes[] memory calls = new bytes[](2);
        calls[0] = abi.encodeCall(midnight.take, (offer, hex"", 400e18, taker, address(0), address(0), hex""));
        calls[1] = abi.encodeCall(midnight.take, (offer, hex"", 400e18, taker, address(0), address(0), hex""));

        vm.prank(taker);
        vm.expectRevert(IMidnight.ConsumedAssets.selector); // second fill reverts
        midnight.multicall(calls);

        // Assert: consumed is at maxAssets (inflated), not 800e18 (actual fills)
        assertEq(midnight.consumed(maker, group), maxAssets);
    }
}
```

**Expected assertions:**
- The `multicall` reverts (entire bundle fails).
- `consumed[maker][group]` equals `maxAssets` (inflated by callback), not the sum of actual fills.
- A single `take` of 400 followed by a separate `take` of 400 also fails on the second call with `ConsumedAssets`.

### Citations

**File:** src/Midnight.sol (L367-369)
```text
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L420-421)
```text
        address buyerCallback = offer.buy ? offer.callback : takerCallback;
        address sellerCallback = offer.buy ? takerCallback : offer.callback;
```

**File:** src/Midnight.sol (L445-453)
```text
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

**File:** certora/specs/Consume.spec (L20-27)
```text
///  Only setConsumed and take can modify the consumed mapping.
rule onlySetConsumedAndTakeChangeConsumed(env e, method f, calldataarg args, address user, bytes32 group) filtered { f -> f.selector != sig:setConsumed(bytes32, uint256, address).selector && f.selector != sig:take(Midnight.Offer, bytes, uint256, address, address, address, bytes).selector } {
    uint256 consumedBefore = consumed(user, group);

    f(e, args);

    assert consumed(user, group) == consumedBefore;
}
```
