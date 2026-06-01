Audit Report

## Title
Zero-asset rounding allows unbounded offer reuse with no token transfer — (`src/Midnight.sol`, `src/libraries/UtilsLib.sol`)

## Summary
When a buy offer has `maxAssets > 0`, the `consumed` mapping is incremented by `buyerAssets`, which is computed via `mulDivDown` and rounds to zero whenever `units * buyerPrice < WAD`. A taker can call `take` with a small enough `units` value to keep `consumed` permanently at zero while still mutating buyer credit and seller debt on every call, with no token transfer required.

## Finding Description

**Root cause — `UtilsLib.mulDivDown` truncates to zero:**

`src/libraries/UtilsLib.sol` line 29–31:
```solidity
function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y) / d;
}
```
When `x * y < d`, the result is 0.

**Vulnerable code path — `src/Midnight.sol` lines 363–373:**
```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // → 0 when units*buyerPrice < WAD
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    //                                                              ^^^^^^^^^^^ += 0
    require(newConsumed <= offer.maxAssets, ConsumedAssets());   // passes unchanged
}
```

For a buy offer, `buyerPrice = offerPrice` (the settlement fee cancels: `sellerPrice = offerPrice − fee`, `buyerPrice = sellerPrice + fee`). The condition for `buyerAssets = 0` is simply `units * offerPrice < WAD`. At low ticks (tick near 0), `tickToPrice` returns very small values (e.g., `~1e9`), so any `units < WAD / offerPrice` (e.g., `units < 1e9`) satisfies this.

**Position state still mutates with `units` (lines 382–414):**
```solidity
uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
uint256 sellerDebtIncrease = units - sellerCreditDecrease;
...
buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);
sellerPos.debt  += UtilsLib.toUint128(sellerDebtIncrease);
```
These use `units` directly, not `buyerAssets`, so credit/debt changes occur even when `buyerAssets = 0`.

**Token transfers are zero (lines 455–456):**
```solidity
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets); // 0
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);                    // 0
```
Both `buyerAssets` and `sellerAssets` are 0 (since `sellerPrice ≤ buyerPrice`), so no tokens move.

**Why existing checks fail:**
- `require(newConsumed <= offer.maxAssets)` only enforces an upper bound; it does not require that `consumed` actually increased.
- There is no `require(units == 0 || buyerAssets > 0)` guard anywhere in `take`.
- The `maxUnits` branch (line 371) uses `+= units` and is immune; only the `maxAssets` branch is affected.

## Impact Explanation
Every call with `units = U > 0` and `buyerAssets = 0`:
- Grants the buyer (maker) `U` units of credit with zero token payment.
- Burdens the seller (taker) with `U` units of debt with zero token receipt.
- Leaves `consumed` unchanged, so the offer is never exhausted.

Repeated indefinitely, this creates unbounded credit for the maker and unbounded debt for the taker without any token backing, directly violating the solvency invariant: the contract's loan-token balance does not cover the credit it has issued.

## Likelihood Explanation
**Preconditions:**
- A buy offer with `maxAssets > 0` must exist (common configuration).
- The offer's tick must be low enough that `offerPrice < WAD` (true for every valid tick) and `units * offerPrice < WAD` for some reachable `units` value (true for any tick where `offerPrice < WAD`, i.e., every valid tick below the maximum).

**Feasibility:** Any unprivileged taker can compute the required `units` off-chain as `U = floor((WAD − 1) / offerPrice)` and call `take` directly. No special permissions, oracle manipulation, or token owner cooperation is required.

**Repeatability:** Unlimited — `consumed` never advances, so the offer never closes.

## Recommendation
Add a guard after computing `buyerAssets`/`sellerAssets` that rejects zero-asset fills when `maxAssets > 0`:

```solidity
if (offer.maxAssets > 0) {
    uint256 assetsDelta = offer.buy ? buyerAssets : sellerAssets;
    require(assetsDelta > 0, ZeroAssetsFill());
    newConsumed = consumed[offer.maker][offer.group] += assetsDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, enforce a global `require(units > 0)` at the top of `take` and additionally require that the computed asset amount is non-zero when `maxAssets > 0`, ensuring `consumed` always strictly increases on every valid fill.

## Proof of Concept
1. Deploy a market with a low-tick buy offer: `offer.buy = true`, `offer.maxAssets = M > 0`, `offer.tick = 0` (giving `offerPrice ≈ 1e9`).
2. Compute `U = (WAD - 1) / offerPrice` (e.g., `U = 1e9 - 1`).
3. Call `take(offer, ..., units=U, taker=attacker, ...)`.
4. Assert: `consumed[maker][group] == 0` (unchanged), `buyerPos.credit == U`, `sellerPos.debt == U`, token balance of contract unchanged.
5. Repeat step 3 arbitrarily many times; each call succeeds and further inflates credit/debt with zero token cost.
6. A fuzz test parameterizing `units` over `[1, WAD/offerPrice - 1]` and asserting `consumed > 0` after each `take` will reliably catch this.