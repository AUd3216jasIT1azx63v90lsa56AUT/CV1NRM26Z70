Audit Report

## Title
Nested `take()` in `onBuy` callback exploits lock/health-check ordering to force outer `take()` to revert with `SellerIsLiquidatable` - (File: src/Midnight.sol)

## Summary
The `take()` function uses a transient liquidation lock to protect the seller during callbacks, but the lock-release and health-check sequence at lines 475–476 is exploitable via reentrancy. A malicious buyer's `onBuy` callback can invoke a nested `take()` on a second sell offer from the same seller, increasing the seller's debt while the lock is held. The nested call's health check passes due to the lock short-circuit; the outer call's health check then fails after the lock is released, reverting the entire transaction with `SellerIsLiquidatable` and wasting the victim's gas.

## Finding Description

**Root cause — lock/health-check ordering in `take()`:**

At line 444, `tExchange` atomically reads the prior lock value and sets it to `true`:

```solidity
bool wasLocked = UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, true);
```

`tExchange` (`src/libraries/UtilsLib.sol` lines 74–80) uses `tload`/`tstore` to atomically swap the transient slot. After both callbacks complete, line 475 conditionally releases the lock and line 476 performs the health check:

```solidity
if (!wasLocked) UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, false);
require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
``` [1](#0-0) [2](#0-1) 

The lock is released **before** the health check in the outer frame, but the nested frame never releases it (because `wasLocked = true` there). This creates an asymmetry: the nested take's health check short-circuits to pass (lock still held), while the outer take's health check runs for real (lock just released).

**Exploit path:**

1. Attacker deploys a malicious contract and posts a buy offer (`offer.buy = true`) with `offer.callback` pointing to it.
2. Victim (seller) calls `take()` on the attacker's buy offer — `seller = taker = victim`.
3. **Outer take, line 444**: `tExchange` returns `false` (not previously locked), sets lock to `true`; `wasLocked = false`.
4. **Outer take, lines 445–453**: attacker's `onBuy` is invoked.
5. **Inside `onBuy`**: attacker's contract calls `take()` on a second sell offer where `offer.maker = victim` (`offer.buy = false`), so `seller = victim` again.
   - **Nested take, line 444**: `tExchange` returns `true` (already locked); `wasLocked = true` in nested frame.
   - **Nested take, line 414**: `sellerPos.debt += sellerDebtIncrease` — victim's debt increases.
   - **Nested take, line 475**: `if (!wasLocked)` → `if (!true)` → false → lock is **not** released.
   - **Nested take, line 476**: `liquidationLocked(id, seller)` returns `true` → health check short-circuits and passes even if seller is now unhealthy.
   - Nested take succeeds.
6. **Back in outer take, line 475**: `if (!wasLocked)` → `if (!false)` → true → lock **is** released (set to `false`).
7. **Outer take, line 476**: `liquidationLocked(id, seller)` returns `false`; `isHealthy` returns `false` (debt was increased by nested take) → **reverts with `SellerIsLiquidatable()`**. [3](#0-2) [4](#0-3) 

The EVM atomically reverts all state changes, including the nested take's debt increase, so no permanent state mutation occurs.

**Existing checks are insufficient:**

The lock mechanism is designed to prevent liquidation during callbacks, not to prevent reentrancy that increases the seller's debt. The Certora formal verification spec (`certora/specs/Reverts.spec` lines 231–233) explicitly adds `require !liquidationLocked(id, seller)` as a precondition to avoid this short-circuit behavior, confirming the formal proofs do not cover the reentrant case. No reentrancy guard exists on `take()`. [5](#0-4) 

## Impact Explanation
No funds are permanently lost — the revert undoes all mutations. The concrete impact is a **targeted, repeatable transaction-level DoS**: any seller whose position can be pushed unhealthy via a second sell offer cannot successfully take the attacker's buy offer. The victim wastes gas on every attempt. If the attacker posts buy offers at competitive prices across multiple ticks, they can selectively and indefinitely block a specific seller from trading with them, forcing the seller to use less favorable offers elsewhere.

## Likelihood Explanation
The attacker requires no privileged access. They need only: (1) post a buy offer with a malicious callback contract (permissionless), and (2) ensure the victim has at least one other active sell offer in the same market whose execution would push the victim's health below the threshold (observable on-chain). The attack is repeatable at negligible net cost — the outer take reverts, so the attacker pays only gas. Any unprivileged user can execute this against any seller meeting the preconditions.

## Recommendation
Add a reentrancy guard on `take()` that prevents nested invocations entirely. A transient boolean flag (separate from the liquidation lock) set at the entry of `take()` and cleared at exit would block the nested call before any state mutation occurs. Alternatively, snapshot `sellerPos.debt` before the callback block and assert it has not increased afterward, independent of the lock state. Do not rely on swapping lines 475–476 alone — doing so would cause the outer take's health check to also short-circuit via the still-held lock, defeating the check entirely. [6](#0-5) 

## Proof of Concept

**Minimal Foundry test plan:**

1. Deploy a `MaliciousCallback` contract implementing `IBuyCallback.onBuy`. Inside `onBuy`, call `midnight.take()` on a pre-posted sell offer where `offer.maker = victim`, with `units` large enough to push the victim's LTV above the liquidation threshold.
2. Attacker calls `midnight.take()` (or posts a buy offer) with `offer.callback = address(MaliciousCallback)`.
3. Victim calls `midnight.take(attackerBuyOffer, ...)`.
4. Assert the transaction reverts with `SellerIsLiquidatable()`.
5. Assert victim's on-chain debt is unchanged (revert undid everything).
6. Repeat step 3 to confirm the DoS is indefinitely repeatable.

The `MaliciousCallback` contract needs only a token approval sufficient to cover the nested take's settlement fee; since the outer revert returns all tokens, the attacker's net token cost is zero. [7](#0-6) [1](#0-0)

### Citations

**File:** src/Midnight.sol (L337-346)
```text
    function take(
        Offer memory offer,
        bytes memory ratifierData,
        uint256 units,
        address taker,
        address receiverIfTakerIsSeller,
        address takerCallback,
        bytes memory takerCallbackData
    ) external returns (uint256, uint256) {
        require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
```

**File:** src/Midnight.sol (L412-414)
```text
        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/Midnight.sol (L444-476)
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

        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);

        if (sellerCallback != address(0)) {
            bytes memory sellerCallbackData = offer.buy ? takerCallbackData : offer.callbackData;
            require(
                ISellCallback(sellerCallback)
                    .onSell(
                        id,
                        offer.market,
                        sellerAssets,
                        units,
                        sellerPendingFeeDecrease,
                        seller,
                        receiver,
                        sellerCallbackData
                    ) == CALLBACK_SUCCESS,
                WrongSellCallbackReturnValue()
            );
        }
        if (!wasLocked) UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, false);
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

**File:** src/libraries/UtilsLib.sol (L74-80)
```text
    function tExchange(uint256 baseSlot, bytes32 key1, address key2, bool value) internal returns (bool previous) {
        uint256 slot = uint256(keccak256(abi.encode(key1, key2, baseSlot)));
        assembly ("memory-safe") {
            previous := tload(slot)
            tstore(slot, value)
        }
    }
```

**File:** certora/specs/Reverts.spec (L231-233)
```text
    // Without this, take's liquidatability check short-circuits to false (without calling isHealthy) because
    // take's tExchange keeps the lock set when wasLocked is true, so the oracle is never queried.
    require !liquidationLocked(id, seller), "seller is not liquidation locked";
```
