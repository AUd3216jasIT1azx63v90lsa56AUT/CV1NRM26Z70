Audit Report

## Title
Zero-asset take bypasses full-consumption guard on buy offers with `buyerPrice < WAD` - (File: `src/Midnight.sol`)

## Summary
When `offer.buy == true` and `offer.maxAssets > 0`, the consumed accounting increments by `buyerAssets = units.mulDivDown(buyerPrice, WAD)`, which truncates to zero when `units * buyerPrice < WAD`. Because the cap check uses `<=`, a taker can repeatedly call `take` with `units > 0` after the offer is fully consumed (`consumed == maxAssets`), passing the guard with `newConsumed = maxAssets + 0 = maxAssets`, and mutating position state (credit, debt, `totalUnits`) without transferring any assets. The test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` explicitly names and demonstrates this as a bug.

## Finding Description

**Root cause — `src/Midnight.sol` lines 363 and 367–369:**

`buyerAssets` is computed with `mulDivDown`, which performs integer division and truncates to zero whenever `units * buyerPrice < WAD`:

```solidity
// line 363
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
```

The consumed cap check then adds this (potentially zero) value and enforces `<=`:

```solidity
// lines 367–369
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

When `consumed == maxAssets` and `buyerAssets == 0`, `newConsumed = maxAssets + 0 = maxAssets ≤ maxAssets` — the check passes unconditionally.

The protocol's own NatSpec at line 333–334 acknowledges the truncation: *"all buyerAssets are reachable only if buyerPrice <= WAD"* — confirming that `buyerAssets` can be zero when `buyerPrice < WAD`.

After the guard, position state mutates unconditionally at lines 408–417:

```solidity
buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);   // line 410
sellerPos.debt   += UtilsLib.toUint128(sellerDebtIncrease);   // line 414
_marketState.totalUnits = UtilsLib.toUint128(
    _marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease); // lines 416–417
```

All three mutate by `units` while zero tokens are transferred.

**Exploit flow:**
1. Attacker creates or uses a buy offer with `buyerPrice < WAD` (any tick where `offerPrice < WAD`, i.e., a negative tick) and `maxAssets > 0`.
2. Attacker fills the offer via legitimate calls until `consumed[maker][group] == maxAssets`.
3. Attacker calls `take(offer, ..., units=1)`. `mulDivDown(1 * buyerPrice, WAD) = 0`. The cap check passes: `maxAssets + 0 ≤ maxAssets`.
4. `buyerPos.credit`, `sellerPos.debt`, and `_marketState.totalUnits` all increment by `units=1` with zero asset transfer.
5. Step 3–4 is repeatable indefinitely on the same fully-consumed offer.

**Why existing checks fail:** There is no floor check requiring `buyerAssets > 0` when `units > 0`. The `<=` guard only prevents `newConsumed` from exceeding `maxAssets`; it does not prevent a zero-increment take from proceeding once the cap is already reached.

## Impact Explanation

A fully-consumed buy offer can be taken an unbounded number of additional times with `units > 0` and zero asset transfer. Each such take manufactures unbacked credit for the maker (buyer) and unbacked debt for the taker (seller), and inflates `_marketState.totalUnits`. This directly violates the core accounting invariant that `consumed == maxAssets` means no further fills are permitted, enabling creation of unbacked credit/debt — a critical integrity failure in the protocol's financial state.

## Likelihood Explanation

All preconditions are reachable by an unprivileged taker:
- `offer.buy == true`, `offer.maxAssets > 0` — standard offer configuration.
- `buyerPrice < WAD` — any tick where `offerPrice < WAD` (i.e., a negative tick); fully attacker-controlled via `offer.tick`.
- `consumed == maxAssets` — reachable by the attacker themselves via prior legitimate fills.

No privileged access, leaked keys, or victim mistakes are required. The attack is repeatable indefinitely on the same offer after full consumption.

## Recommendation

Add an explicit floor check before the consumed accounting to reject zero-asset takes when `units > 0`:

```solidity
// In the offer.buy && offer.maxAssets > 0 branch, before updating consumed:
require(buyerAssets > 0, ZeroAssetTake());
```

Alternatively, change the cap check from `<=` to `<` **and** add the zero-asset floor. The `<=` change alone is insufficient because it still permits the zero-increment when `consumed < maxAssets`. The zero-asset floor is the necessary and sufficient fix: it ensures that any `take` with `units > 0` must result in a non-zero asset transfer, preserving the accounting invariant.

## Proof of Concept

The test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` is a named, existing reproduction. Minimal manual steps:

1. Deploy the protocol and create a buy offer with `tick` set to a negative value such that `offerPrice < WAD`, and `maxAssets = N` for some small `N`.
2. Call `take` with enough `units` to bring `consumed` to exactly `maxAssets = N`.
3. Call `take` again with `units = 1`. Observe: the call succeeds, `consumed` remains `N`, but `buyerPos.credit` and `sellerPos.debt` each increase by 1, and `totalUnits` increases by 1, with zero tokens transferred.
4. Repeat step 3 arbitrarily many times to manufacture unbounded unbacked credit/debt.