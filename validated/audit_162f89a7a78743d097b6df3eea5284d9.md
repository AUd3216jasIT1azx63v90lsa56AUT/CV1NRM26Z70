Audit Report

## Title
Zero-unit take on fully-consumed buy offer invokes maker's `IBuyCallback.onBuy` with zero assets — (File: src/Midnight.sol)

## Summary
When a buy offer with `maxAssets > 0` is fully consumed, any unprivileged caller can call `take` with `units = 0`, causing `buyerAssets = 0` and `newConsumed = maxAssets + 0 = maxAssets`, which satisfies the `<=` guard at line 369. Execution then proceeds to invoke the maker's registered `IBuyCallback.onBuy` with `buyerAssets = 0` and `units = 0`. This is repeatable indefinitely at gas cost only, violating the protocol invariant that a fully-consumed offer must not allow further interaction.

## Finding Description

**Root cause — no `units > 0` guard in `take`:**

Confirmed by grep: there is no `require(units > 0)` anywhere in `src/Midnight.sol`.

**Consumed check passes with zero addition (`src/Midnight.sol` lines 363–369):**

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
``` [1](#0-0) 

The guard is `<=`, not `<`. Adding zero to an already-maxed `consumed` value passes the check.

**Callback invocation (`src/Midnight.sol` lines 420, 445–453):**

```solidity
address buyerCallback = offer.buy ? offer.callback : takerCallback;

if (buyerCallback != address(0)) {
    require(
        IBuyCallback(buyerCallback)
            .onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
        == CALLBACK_SUCCESS, ...
    );
}
``` [2](#0-1) 

No `require(units > 0)` precedes this block.

**`safeTransferFrom` with zero (`src/Midnight.sol` lines 455–456):**

```solidity
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets); // 0
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets); // 0
``` [3](#0-2) 

Standard ERC-20 tokens accept zero-amount transfers; these do not revert.

**Exploit flow:**
1. Maker creates a buy offer: `offer.buy = true`, `offer.maxAssets = M > 0`, `offer.callback = <MakerCallbackContract>`.
2. Legitimate taker fills it: `take(..., units=U)` where `buyerAssets = M`. Now `consumed[maker][group] = M`.
3. Attacker (any address ≠ maker) calls `take(..., units=0)`:
   - `buyerAssets = 0`, `sellerAssets = 0`
   - `newConsumed = M + 0 = M ≤ M` → passes
   - All position-delta checks pass trivially (`buyerCreditIncrease = 0`, `sellerDebtIncrease = 0`)
   - `IBuyCallback(offer.callback).onBuy(..., buyerAssets=0, units=0, ...)` is invoked
   - `safeTransferFrom(..., 0)` executes and succeeds
4. Step 3 is repeatable indefinitely.

**Why existing checks fail:**

- No `require(units > 0)` guard in `take`.
- The `ConsumedAssets` check is `newConsumed <= maxAssets`, not `consumed_before < maxAssets`.
- Certora spec `fullyConsumedOfferRevertsOnNonTrivialTake` (`certora/specs/Consume.spec` lines 99–111) only covers the `maxUnits` branch (`require offer.maxAssets == 0`); no equivalent rule exists for the `maxAssets` branch. [4](#0-3) 

- Spec `takeConsumedAtMaxUnchangedAssets` (`certora/specs/Consume.spec` lines 88–97) only asserts that `consumed` does not change — it does not assert that callbacks are not invoked. [5](#0-4) 

## Impact Explanation
An unprivileged attacker can invoke the maker's `IBuyCallback.onBuy` with `buyerAssets = 0` and `units = 0` on an already fully-consumed buy offer, an unlimited number of times. Maker callback contracts that track fills, update internal accounting, emit events, or execute conditional logic on each `onBuy` invocation will process spurious zero-asset callbacks indistinguishable from legitimate ones. This constitutes unauthorized state mutation in maker-side integrations and breaks the protocol invariant that a fully-consumed offer must not allow any further interaction.

## Likelihood Explanation
Preconditions are minimal: any buy offer with `maxAssets > 0` and a non-zero `offer.callback` is vulnerable once fully consumed. The attacker requires no special privilege — only the ability to call `take` as any address that is not the maker. The attack is repeatable indefinitely at gas cost only, with no capital requirement.

## Recommendation
Add a `require(units > 0)` guard at the top of `take`, before any state reads or writes:

```solidity
require(units > 0, ZeroUnits());
```

Alternatively, tighten the consumed check to `<` instead of `<=`:

```solidity
require(newConsumed < offer.maxAssets || buyerAssets == 0, ConsumedAssets());
```

However, the cleanest fix is the `require(units > 0)` guard, which also closes the analogous zero-unit path in the `maxUnits` branch and eliminates all zero-transfer side effects.

Additionally, add a Certora rule analogous to `fullyConsumedOfferRevertsOnNonTrivialTake` for the `maxAssets` branch:

```
rule fullyConsumedOfferRevertsOnNonTrivialTakeAssets(...) {
    require offer.maxUnits == 0;
    require offer.maxAssets > 0 && consumedBefore >= offer.maxAssets;
    take(e, offer, ratifierData, units, ...);
    assert units == 0;
}
```

## Proof of Concept

```solidity
// 1. Deploy a MakerCallback that counts onBuy invocations.
// 2. Maker creates buy offer: maxAssets = 1000e18, callback = MakerCallback.
// 3. Taker calls take(offer, ..., units=U) such that buyerAssets = 1000e18.
//    → consumed[maker][group] = 1000e18 = maxAssets.
// 4. Attacker calls take(offer, ..., units=0) from any address ≠ maker.
//    → buyerAssets = 0, newConsumed = 1000e18 ≤ 1000e18 → passes.
//    → MakerCallback.onBuy(id, market, 0, 0, 0, buyer, data) is called.
//    → MakerCallback.invocationCount increments.
// 5. Repeat step 4 N times → invocationCount = N.
// Assert: invocationCount > 1 after a fully-consumed offer.
```

### Citations

**File:** src/Midnight.sol (L363-369)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L420-453)
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
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** certora/specs/Consume.spec (L88-97)
```text
/// If consumed is already at or above maxAssets before a take in assets mode, it remains unchanged.
rule takeConsumedAtMaxUnchangedAssets(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxUnits == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert consumedBefore >= offer.maxAssets => consumed(offer.maker, offer.group) == consumedBefore;
}
```

**File:** certora/specs/Consume.spec (L99-111)
```text
/// A fully-consumed offer in units mode only allows no-op takes.
rule fullyConsumedOfferRevertsOnNonTrivialTake(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxAssets == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    require offer.maxUnits > 0 && consumedBefore >= offer.maxUnits, "assume the offer is fully consumed";

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    // If take does not revert, its input has to be zero.
    assert units == 0;
}
```
