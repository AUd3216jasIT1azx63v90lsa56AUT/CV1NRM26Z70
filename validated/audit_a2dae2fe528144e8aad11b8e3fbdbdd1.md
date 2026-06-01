The code confirms the vulnerability. Let me verify one more detail about the transfer behavior with zero amounts.

Audit Report

## Title
Zero-unit take on fully-consumed buy offer invokes maker's `IBuyCallback.onBuy` with zero assets — (File: src/Midnight.sol)

## Summary
When a buy offer with `maxAssets > 0` is fully consumed, an attacker can call `take` with `units = 0`, causing `buyerAssets = 0` and `newConsumed = maxAssets + 0 = maxAssets`, which satisfies the `newConsumed <= maxAssets` guard. Execution then proceeds to invoke the maker's registered `IBuyCallback.onBuy` with `buyerAssets = 0` and `units = 0`. This can be repeated indefinitely by any unprivileged caller at gas cost only, violating the invariant that a fully-consumed offer must not allow further interaction.

## Finding Description

**Root cause — `src/Midnight.sol` lines 363–369:**

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // units=0 → buyerAssets=0
    : units.mulDivUp(buyerPrice, WAD);

uint256 newConsumed;
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    // consumed += 0  →  newConsumed == maxAssets
    require(newConsumed <= offer.maxAssets, ConsumedAssets());   // maxAssets <= maxAssets → PASSES
}
```

The guard is `<=` (not `<`), so adding zero to an already-maxed `consumed` value passes the check.

**Callback invocation — `src/Midnight.sol` lines 420 and 445–453:**

```solidity
address buyerCallback = offer.buy ? offer.callback : takerCallback;
// For a buy offer: buyerCallback = offer.callback (the maker's callback)

if (buyerCallback != address(0)) {
    require(
        IBuyCallback(buyerCallback)
            .onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
        == CALLBACK_SUCCESS, ...
    );
}
```

There is no `require(units > 0)` guard anywhere in `take`.

**Exploit flow:**
1. Maker creates a buy offer: `offer.buy = true`, `offer.maxAssets = M > 0`, `offer.callback = <MakerCallbackContract>`.
2. Legitimate taker fills it: `take(..., units=U)` where `buyerAssets = M`. Now `consumed[maker][group] = M`.
3. Attacker (any address ≠ maker) calls `take(..., units=0)`:
   - `buyerAssets = 0`, `sellerAssets = 0`
   - `newConsumed = M + 0 = M ≤ M` → `ConsumedAssets` check passes
   - All position-delta checks pass trivially (`buyerCreditIncrease = 0`, `sellerDebtIncrease = 0`)
   - `IBuyCallback(offer.callback).onBuy(..., buyerAssets=0, units=0, ...)` is invoked
   - `safeTransferFrom(..., 0)` executes (succeeds for standard ERC-20 tokens)
4. Step 3 is repeatable indefinitely.

**Why existing checks fail:**
- No `require(units > 0)` guard in `take`.
- The `ConsumedAssets` check is `newConsumed <= maxAssets`, not `consumed_before < maxAssets`.
- The Certora spec `fullyConsumedOfferRevertsOnNonTrivialTake` (`certora/specs/Consume.spec` lines 99–111) only covers the `maxUnits` branch (`require offer.maxAssets == 0`); there is no equivalent rule for the `maxAssets` branch.
- The spec `takeConsumedAtMaxUnchangedAssets` (`certora/specs/Consume.spec` lines 88–97) only asserts that `consumed` does not change — it does not assert that callbacks are not invoked.

## Impact Explanation
An unprivileged attacker can invoke the maker's `IBuyCallback.onBuy` with `buyerAssets = 0` and `units = 0` on an already fully-consumed buy offer, an unlimited number of times. Maker callback contracts that track fills, update internal accounting, emit events, or execute conditional logic on each `onBuy` invocation will process spurious zero-asset callbacks indistinguishable from legitimate ones. This breaks the protocol invariant that a fully-consumed offer must not allow any further interaction, and can corrupt the state of any maker-side callback integration.

## Likelihood Explanation
Preconditions are minimal: any buy offer with `maxAssets > 0` and a non-zero `offer.callback` is vulnerable once fully consumed. The attacker requires no special privilege — only the ability to call `take` as any address that is not the maker. The attack is repeatable indefinitely at gas cost only, with no capital requirement.

## Recommendation
Add a `require(units > 0)` guard at the top of `take`, or change the `ConsumedAssets` check to use strict inequality: `require(consumed_before < offer.maxAssets, ConsumedAssets())` (i.e., check the pre-addition value). The simplest and most robust fix is:

```solidity
require(units > 0, ZeroUnits());
```

placed immediately after the authorization checks in `take`. This also closes the analogous zero-unit path for the `maxUnits` branch and for sell offers.

Additionally, add a Certora rule analogous to `fullyConsumedOfferRevertsOnNonTrivialTake` for the `maxAssets` branch to close the formal verification gap.

## Proof of Concept
```solidity
// 1. Deploy a MakerCallback that counts onBuy invocations.
// 2. Maker creates: offer.buy=true, offer.maxAssets=1e18, offer.callback=MakerCallback.
// 3. Taker fills fully: take(offer, ..., units=U) such that buyerAssets == 1e18.
//    Assert: consumed[maker][group] == 1e18, MakerCallback.callCount == 1.
// 4. Attacker calls: take(offer, ..., units=0).
//    Assert: call succeeds (no revert), MakerCallback.callCount == 2.
// 5. Repeat step 4 N times.
//    Assert: MakerCallback.callCount == 2 + N.
```