Audit Report

## Title
Zero-asset take bypasses fully-consumed cap on assets-based buy offers, mutating position state and triggering maker callback — (`src/Midnight.sol`)

## Summary
When a buy offer uses `maxAssets`-based consumption and `offerPrice < WAD`, a taker can call `take()` with `units = 1` such that `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`. Because the consumed accounting increments by zero, the cap check passes even when the offer is fully exhausted. The take proceeds to completion: position credit and debt are mutated, `totalUnits` increases, the maker's `onBuy` callback fires, and zero tokens are transferred — all on an offer that should be inert. The attack is repeatable without bound.

## Finding Description
**Root cause** — `src/Midnight.sol`, `take()`:

```solidity
// Line 363
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // mulDivDown(1, <WAD, WAD) = 0
    : units.mulDivUp(buyerPrice, WAD);

// Lines 367–369
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += buyerAssets; // += 0
    require(newConsumed <= offer.maxAssets, ConsumedAssets());       // maxAssets <= maxAssets ✓
}
```

The sole guard preventing execution on an exhausted offer is the `ConsumedAssets` check at line 369. Because `buyerAssets` rounds to zero via `mulDivDown`, `newConsumed` is unchanged and the require is trivially satisfied regardless of prior consumption.

**Execution continues unconditionally after the guard:**
- Lines 382–384: `buyerCreditIncrease = zeroFloorSub(1, buyerPos.debt)` — equals `1` if buyer has no debt; `sellerDebtIncrease = 1 - sellerCreditDecrease`.
- Lines 408–414: `buyerPos.credit += 1`, `sellerPos.debt += 1`.
- Lines 416–417: `_marketState.totalUnits` increases by `buyerCreditIncrease`.
- Lines 445–453: `IBuyCallback(buyerCallback).onBuy(...)` fires with `buyerAssets=0, units=1`.
- Lines 455–456: `safeTransferFrom(..., 0)` — zero tokens transferred, no revert.

**Why existing checks fail:**
There is no `require(units == 0 || buyerAssets > 0)` guard. The `ConsumedAssets` check only compares `newConsumed` to `maxAssets`; it cannot detect a zero-increment bypass. The protocol's own NatDoc at line 94 explicitly acknowledges this: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."*

## Impact Explanation
An unprivileged taker can, on any fully-consumed assets-based buy offer with `offerPrice < WAD`:

1. **Corrupt position accounting**: Grant the maker free credit and impose free debt on the taker/seller with zero token cost — `totalUnits` and position state diverge from actual asset flows.
2. **Invoke maker callback arbitrarily**: `onBuy` fires with `buyerAssets=0, units=1`; callback logic that assumes it is only called when real assets are exchanged may behave incorrectly or be exploited.
3. **Repeat without bound**: `consumed` never increases past `maxAssets`, so the attack is repeatable indefinitely, amplifying position mutation and callback invocations at zero cost.

This constitutes unauthorized state mutation and unauthorized callback invocation — both in-scope impact classes per RESEARCHER.md.

## Likelihood Explanation
All preconditions are reachable by an unprivileged user with no special access:
- `offer.buy = true`, `offer.maxAssets > 0` — standard buy offer configuration.
- `offerPrice < WAD` — any tick below the WAD-price tick; the confirmed test uses `MAX_TICK - 16`.
- `consumed[maker][group] == maxAssets` — reachable after a normal full fill, or self-set by the maker via `setConsumed`.
- Zero capital required: no tokens are transferred.

The attack is deterministic, requires no timing, and is repeatable on every qualifying exhausted offer.

## Recommendation
Add a guard immediately after computing `buyerAssets` (and symmetrically `sellerAssets` for sell offers) to reject zero-asset takes when `units > 0`:

```solidity
// After line 363–364, before the cap check:
if (offer.maxAssets > 0) {
    uint256 relevantAssets = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || relevantAssets > 0, ZeroAssetTake());
}
```

Alternatively, enforce a minimum `units` value such that the computed assets are always nonzero for any valid `offerPrice`, or reject takes where `buyerAssets == 0 && units > 0` unconditionally.

## Proof of Concept
The existing test `testBugBuyMaxAssetsBypass()` in `test/TakeTest.sol` (lines 858–889) is a confirmed, passing reproduction. It:
1. Sets `lenderOffer.maxAssets = 1`, `lenderOffer.tick = MAX_TICK - 16` (so `offerPrice < WAD`).
2. Calls `midnight.setConsumed(lenderOffer.group, lenderOffer.maxAssets, lender)` to fully exhaust the offer.
3. Calls `take(1, borrower, lenderOffer)` — no `vm.expectRevert`.
4. Asserts `buyerAssets == 0`, `sellerAssets == 0`, `consumed` unchanged, token balances unchanged.
5. Asserts `creditOf(lender) > before`, `debtOf(borrower) > before`, `totalUnits > before`.

The test passes, confirming the exploit path end-to-end.