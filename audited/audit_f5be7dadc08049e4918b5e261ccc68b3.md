All code references verified against `src/Midnight.sol`. Every line number, function signature, and logic path in the submitted report matches the actual code exactly. No reentrancy guard exists on `setConsumed`. The `multicall` function exists in the interface. The AUTHORIZATIONS natspec at lines 101–110 confirms authorized accounts can call any function but provides no on-chain enforcement. The finding is valid.

---

Audit Report

## Title
Maker's `onBuy` callback can call `setConsumed` to inflate `consumed` and block subsequent fills - (File: `src/Midnight.sol`)

## Summary
In `take()`, `consumed[offer.maker][offer.group]` is incremented and the cap is enforced at lines 366–373 before the maker-controlled `onBuy` callback fires at lines 445–453. Because `setConsumed` (lines 723–728) only requires the caller to be authorized by `onBehalf` and the new amount to be non-decreasing, a maker who pre-authorizes their callback contract can have that callback call `setConsumed(group, maxAssets, maker)` during `onBuy`, inflating `consumed` to the offer cap. Any subsequent fill of the same offer or any offer sharing the same group within the same transaction will then revert with `ConsumedAssets`/`ConsumedUnits`.

## Finding Description

**Root cause and code path:**

`take()` at lines 366–373 increments `consumed` and enforces the cap before any callback:

```solidity
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
} else {
    newConsumed = consumed[offer.maker][offer.group] += units;
    require(newConsumed <= offer.maxUnits, ConsumedUnits());
}
```

For a buy offer, `buyerCallback` is set to `offer.callback` (maker-controlled) at line 420:

```solidity
address buyerCallback = offer.buy ? offer.callback : takerCallback;
```

The `onBuy` callback is invoked at lines 445–453, **after** `consumed` has already been written to storage. At this point, the callback can call back into `setConsumed`.

`setConsumed` at lines 723–728 only checks authorization and that the new amount is non-decreasing:

```solidity
function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    require(amount >= consumed[onBehalf][group], AlreadyConsumed());
    consumed[onBehalf][group] = amount;
    ...
}
```

There is no reentrancy guard on `setConsumed`, no snapshot/restore of `consumed` around the callback, and no check that `consumed` has not been externally modified after the callback returns.

**Exploit flow:**

1. Maker deploys `MaliciousCallback` implementing `IBuyCallback`.
2. Maker calls `setIsAuthorized(MaliciousCallback, true, maker)`.
3. Maker creates a buy offer with `offer.callback = MaliciousCallback`, `offer.maxAssets = M`, `offer.group = G`.
4. Taker calls `take(offer, ..., units1)` (partial fill, `units1 < M`):
   - `consumed[maker][G] += buyerAssets1` → e.g., 50. Check passes (50 ≤ 100).
   - `onBuy` is called on `MaliciousCallback`.
   - Inside `onBuy`, callback calls `setConsumed(G, 100, maker)`.
   - `setConsumed` checks: `isAuthorized[maker][MaliciousCallback]` = true ✓; `100 >= 50` ✓. Sets `consumed[maker][G] = 100`.
   - First `take()` completes successfully.
5. Taker (or bundler) calls `take(offer, ..., units2)` in the same transaction via `multicall`:
   - `consumed[maker][G] += buyerAssets2` → 100 + delta > 100. Reverts with `ConsumedAssets`.

**Why existing checks fail:**

- `AlreadyConsumed` in `setConsumed` only prevents *decreasing* consumed, not inflating it to the cap.
- There is no reentrancy guard preventing `setConsumed` from being called during `take`.
- The AUTHORIZATIONS natspec (lines 101–110) warns makers to scope what they authorize, but provides no on-chain enforcement preventing a maker-authorized callback from calling `setConsumed` mid-`take()`.

## Impact Explanation
A maker can use their `onBuy` callback to inflate `consumed[maker][group]` to `maxAssets` after a partial fill. In a `multicall` context, the second `take` reverts, causing the entire bundle to revert — the taker receives nothing and loses gas. In a try/catch context, the taker silently receives fewer units than intended with no on-chain signal of the shortfall. The maker can selectively allow partial fills while atomically blocking further fills, griefing takers who rely on filling the full offer in one transaction. No funds are directly stolen, but the consumed accounting invariant is manipulable by the maker against the taker's interests.

## Likelihood Explanation
All preconditions are fully attacker-controlled: the maker chooses `offer.callback`, `offer.group`, `offer.maxAssets`, and pre-authorizes the callback via `setIsAuthorized`. No special privileges, oracle manipulation, or external conditions are required. The attack is repeatable on every `take` that triggers the callback and requires no coordination beyond deploying a simple callback contract.

## Recommendation
Snapshot `consumed[offer.maker][offer.group]` before the callback and assert it has not increased beyond the pre-callback value after the callback returns, or cache the pre-callback value and use it as the authoritative cap for the remainder of `take()`. Alternatively, prevent `setConsumed` from being called reentrantly during `take()` by recording a per-maker-group lock in transient storage for the duration of the call.

## Proof of Concept
1. Deploy `MaliciousCallback` that, in `onBuy`, calls `Midnight.setConsumed(G, maxAssets, maker)`.
2. Maker calls `setIsAuthorized(address(MaliciousCallback), true, maker)`.
3. Maker creates offer: `buy=true`, `group=G`, `maxAssets=100e18`, `callback=address(MaliciousCallback)`.
4. Construct a `multicall` with two `take` calls: `take(offer, ..., 40e18_units)` then `take(offer, ..., 40e18_units)`.
5. Submit the multicall. Observe: first `take` succeeds internally but `MaliciousCallback.onBuy` sets `consumed[maker][G] = 100e18`; second `take` reverts with `ConsumedAssets`; entire multicall reverts.
6. Repeat with try/catch wrapper: first `take` succeeds (taker receives partial fill), second `take` reverts silently, taker receives half the intended units. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/Midnight.sol (L101-110)
```text
/// AUTHORIZATIONS
/// @dev All functions that change the position, consumed and authorization are accessible to the user and to
/// any account that has been authorized. Thus, to scope authorizations one should authorize a smart-contract with
/// scoped behavior.
/// @dev When authorizing a smart-contract, one should consider:
/// - The targets/functions that the account can call. At least Midnight's functions should be considered, but other
/// contracts might re-use Midnight's authorization mapping too (e.g ratifiers and authorizers). In particular,
/// authorized accounts can authorize other accounts on behalf of the user.
/// - Under which conditions the account can return CALLBACK_SUCCESS when its isRatified function is called.
/// @dev updatePosition and liquidate (for liquidatable users) also impact the position and are permissionless.
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

**File:** src/Midnight.sol (L420-420)
```text
        address buyerCallback = offer.buy ? offer.callback : takerCallback;
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
