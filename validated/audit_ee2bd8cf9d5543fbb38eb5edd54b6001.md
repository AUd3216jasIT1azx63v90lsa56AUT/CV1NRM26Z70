### Title
Batch processing via `multicall` lacks NoThrow variant, enabling griefing by malicious makers — (File: src/Midnight.sol)

---

### Summary

The `multicall` function in `Midnight.sol` processes an array of delegatecalls atomically: if any single call reverts, the entire batch reverts. A malicious maker can exploit this by front-running a victim's pending `multicall` with a `setConsumed(group, type(uint256).max, maker)` call to cancel their own offer, causing the victim's entire batch to fail and forcing a gas-wasting resubmission.

---

### Finding Description

**Root cause — no error isolation in `multicall`:**

`multicall` iterates over calls and propagates any revert immediately: [1](#0-0) 

There is no `try/catch` or skip-on-failure logic. One failing sub-call kills the entire batch.

**Attacker-controlled cancellation primitive:**

Any maker can atomically cancel all their offers in a group by calling `setConsumed` with `type(uint256).max`: [2](#0-1) 

This is a permissionless, zero-cost operation for the maker.

**How cancellation causes `take` to revert:**

Inside `take`, the consumed counter is incremented and checked: [3](#0-2) 

If `consumed[maker][group]` was set to `type(uint256).max` by the attacker's front-run, the `+= units` addition overflows (Solidity 0.8 checked arithmetic), causing an unconditional revert. This revert propagates up through `multicall`'s assembly bubble, reverting the entire batch.

**End-to-end exploit flow:**

1. Attacker (maker) posts a valid offer with `group = G`, `maxUnits = X`.
2. Victim constructs a `multicall` containing `take(attackerOffer, ...)` alongside several legitimate `take` calls.
3. Victim broadcasts the transaction; attacker observes it in the mempool.
4. Attacker front-runs with `setConsumed(G, type(uint256).max, attacker)` — sets `consumed[attacker][G] = type(uint256).max`.
5. Victim's `multicall` executes; `take(attackerOffer, ...)` overflows on the consumed increment and reverts.
6. The entire `multicall` reverts. All legitimate takes also fail.

---

### Impact Explanation

- Every operation bundled with the malicious offer is atomically undone: legitimate takes, collateral operations, repayments — all fail.
- The victim loses the gas cost of the entire multicall and must resubmit after identifying and removing the poisoned offer.
- If the multicall is used by a liquidation bot to atomically take offers and liquidate an unhealthy position, the griefing delays or prevents the liquidation, potentially allowing bad debt to accumulate in the market.
- The attack is repeatable: the attacker can re-list a fresh offer and repeat the cycle indefinitely against the same victim.

---

### Likelihood Explanation

- **No privilege required.** Any address can be a maker and call `setConsumed` on their own group.
- **Mempool visibility.** On Ethereum mainnet the pending `multicall` is publicly visible, giving the attacker a clear front-running window.
- **Zero cost to attacker.** Cancelling via `setConsumed` costs only gas; the attacker suffers no economic penalty.
- **Targeted.** The attacker only needs one offer included in the victim's batch to poison the whole call.

---

### Recommendation

**Short term:** Introduce a `tryMulticall` (NoThrow) variant that wraps each delegatecall in a success check and skips (or records) failures rather than reverting the entire batch:

```solidity
function tryMulticall(bytes[] calldata calls) external returns (bool[] memory successes) {
    successes = new bool[](calls.length);
    for (uint256 i = 0; i < calls.length; i++) {
        (successes[i], ) = address(this).delegatecall(calls[i]);
    }
}
```

**Long term:** When composing batches that include offers from untrusted makers, callers should use `MidnightBundles.sol` functions (which already wrap individual `take` calls in `try/catch`) rather than raw `multicall`. Document this distinction explicitly.

---

### Proof of Concept

```
Setup:
  - attacker = address(0xDEAD), maker of offer O in group G, maxUnits = 1000
  - victim = address(0xBEEF), wants to take O plus two other legitimate offers

Step 1 — victim constructs multicall:
  calls[0] = abi.encodeCall(Midnight.take, (O, ratifierData, 500, victim, ...))
  calls[1] = abi.encodeCall(Midnight.take, (legitimateOffer1, ...))
  calls[2] = abi.encodeCall(Midnight.take, (legitimateOffer2, ...))
  victim broadcasts: midnight.multicall(calls)

Step 2 — attacker front-runs (higher gas):
  midnight.setConsumed(G, type(uint256).max, attacker)
  // consumed[attacker][G] = 2^256 - 1

Step 3 — victim's multicall executes:
  calls[0]: consumed[attacker][G] += 500  →  arithmetic overflow  →  REVERT
  // entire multicall reverts; calls[1] and calls[2] never execute

Result:
  - victim loses ~200k gas
  - all three takes fail
  - attacker can repeat with a new offer
```

### Citations

**File:** src/Midnight.sol (L211-220)
```text
    function multicall(bytes[] calldata calls) external {
        for (uint256 i = 0; i < calls.length; i++) {
            (bool success, bytes memory returnData) = address(this).delegatecall(calls[i]);
            if (!success) {
                assembly ("memory-safe") {
                    revert(add(returnData, 0x20), mload(returnData))
                }
            }
        }
    }
```

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

**File:** src/Midnight.sol (L722-728)
```text
    /// @dev Passing type(uint256).max cancels all offers in the group (and never reverts).
    function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        require(amount >= consumed[onBehalf][group], AlreadyConsumed());
        consumed[onBehalf][group] = amount;
        emit EventsLib.SetConsumed(msg.sender, group, amount, onBehalf);
    }
```
