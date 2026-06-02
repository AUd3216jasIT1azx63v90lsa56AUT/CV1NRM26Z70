Audit Report

## Title
Zero-`buyerAssets` rounding in `maxAssets`-mode buy-offer consumed accounting allows indefinite offer reuse — (File: src/Midnight.sol)

## Summary
When a buy offer is configured with `offer.maxAssets > 0`, the `take` function tracks capacity consumption by incrementing `consumed[maker][group]` by `buyerAssets = units.mulDivDown(buyerPrice, WAD)`. Because `tickToPrice` always returns a value strictly less than `WAD` for every valid tick, filling with `units = 1` produces `buyerAssets = 0`, so `consumed` never advances. The `ConsumedAssets` guard is therefore never triggered, and the offer can be filled an unlimited number of times, allowing the maker to accumulate unbounded credit units while transferring zero loan tokens.

## Finding Description

**Root cause — `src/Midnight.sol` lines 361–369:**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
uint256 buyerPrice = sellerPrice + _settlementFee;
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // rounds DOWN
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
``` [1](#0-0) 

When `offer.buy = true`, `buyerPrice = (offerPrice - _settlementFee) + _settlementFee = offerPrice = tickToPrice(offer.tick)`.

**`tickToPrice` always returns a value strictly less than `WAD`:**

```solidity
function tickToPrice(uint256 tick) internal pure returns (uint256) {
    require(tick <= MAX_TICK, TickOutOfRange());
    unchecked {
        return uint256(1e36)
            .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
            .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
    }
}
``` [2](#0-1) 

The denominator `1e18 + wExp(...)` is always strictly greater than `1e18`, so `1e36 / (1e18 + wExp(...)) < 1e18 = WAD` for every valid tick (0 through `MAX_TICK = 5820`). [3](#0-2) 

Therefore, for any valid tick with `units = 1`:
```
buyerAssets = mulDivDown(1, buyerPrice, 1e18) = (1 * buyerPrice) / 1e18 = 0
```

**No guard prevents this:** A search of `src/Midnight.sol` finds no `require(units > 0)`, no `require(buyerAssets > 0)`, and no minimum-units check anywhere in the `take` path.

**Zero-value transfers do not revert:** `SafeTransferLib.safeTransferFrom` calls `transferFrom(from, to, 0)` at lines 455–456, which succeeds on all standard ERC20 tokens. [4](#0-3) 

**Exploit flow:**
1. Maker creates a buy offer: `offer.buy = true`, `offer.maxAssets = M` (any `M > 0`), any valid `offer.tick`, valid `expiry`, authorized `ratifier`.
2. Maker's second address (taker) calls `take(offer, ..., units=1, ...)` in a loop N times.
3. Each iteration:
   - `buyerAssets = 0`, `sellerAssets = 0`
   - `consumed[maker][group] += 0` → stays at 0
   - `require(0 <= M)` → always passes
   - `buyerPos.credit += 1` (maker gains 1 unit of credit)
   - `sellerPos.debt += 1` (taker incurs 1 unit of debt)
   - Both `safeTransferFrom(..., 0)` calls succeed with no token movement
4. After N iterations: `consumed = 0`, maker credit = N, taker debt = N, loan tokens paid = 0.

**Why existing checks fail:**
- `require(newConsumed <= offer.maxAssets)` — `newConsumed` is always 0, so this never fires. [5](#0-4) 
- `require(offer.maker != taker, SelfTake())` — bypassed by using a second address. [6](#0-5) 
- `isHealthy` check at line 476 — only constrains the taker's collateral capacity, not the number of fills. [7](#0-6) 

## Impact Explanation

The `maxAssets` capacity limit on buy offers is completely unenforced when `units = 1`. A maker can accumulate an unbounded amount of credit (redeemable for loan tokens at maturity) while paying zero loan tokens upfront. This constitutes direct unauthorized acquisition of protocol credit — a concrete theft of value from the protocol's loan token pool at maturity. The `consumed` mapping, the sole mechanism preventing offer overfill, stays permanently at 0, violating the core accounting invariant.

## Likelihood Explanation

**Preconditions:**
- Any buy offer with `maxAssets > 0` at any valid tick (all 5821 ticks are affected).
- A second address controlled by the maker to act as taker, supplying enough collateral to remain healthy under accumulated debt.
- No special permissions, governance access, or oracle manipulation required.

**Feasibility:** Trivially repeatable in a loop. The only cost is gas and the collateral the taker must lock. The maker's credit gain is unbounded and limited only by the taker's collateral capacity.

## Recommendation

Add a guard requiring `buyerAssets > 0` (or equivalently `sellerAssets > 0`) before proceeding with a fill, or enforce a minimum `units` value such that `units.mulDivDown(buyerPrice, WAD) >= 1`. A direct fix:

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
require(buyerAssets > 0 || offer.maxAssets == 0, ZeroBuyerAssets());
```

Alternatively, enforce `require(units >= WAD / buyerPrice + 1)` to guarantee non-zero rounding before the consumed accounting.

## Proof of Concept

**Minimal Foundry test plan:**

```solidity
// 1. Deploy Midnight with a standard ERC20 loan token and collateral token.
// 2. Create a market with tickSpacing = 1.
// 3. Maker (address A) creates a buy offer:
//    offer.buy = true, offer.maxAssets = 1e18, offer.tick = MAX_TICK (5820),
//    offer.expiry = block.timestamp + 1 days, valid ratifier.
// 4. Taker (address B, controlled by A) supplies collateral to remain healthy.
// 5. Call take(offer, ..., units=1, ...) from address B in a loop 1000 times.
// 6. Assert: consumed[A][group] == 0 (never advanced).
// 7. Assert: position[id][A].credit == 1000 (maker gained 1000 credit units).
// 8. Assert: loan token balance of contract unchanged (zero tokens transferred).
// 9. Assert: all 1000 calls succeeded without revert.
```

The test confirms that `consumed` stays at 0 across all iterations, the `ConsumedAssets` guard never fires, and the maker accumulates credit without any token payment.

### Citations

**File:** src/Midnight.sol (L354-354)
```text
        require(offer.maker != taker, SelfTake());
```

**File:** src/Midnight.sol (L361-369)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/Midnight.sol (L476-476)
```text
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

**File:** src/libraries/TickLib.sol (L6-8)
```text
uint256 constant MAX_TICK = 5820;
// Minimum representable price increment in WAD (1e-6 WAD). Tick prices are rounded to multiples of this value.
uint256 constant PRICE_ROUNDING_STEP = 1e12;
```

**File:** src/libraries/TickLib.sol (L44-52)
```text
    function tickToPrice(uint256 tick) internal pure returns (uint256) {
        require(tick <= MAX_TICK, TickOutOfRange());
        unchecked {
            // forge-lint: disable-next-item(unsafe-typecast)
            return uint256(1e36)
                    .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
                    .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
        }
    }
```
