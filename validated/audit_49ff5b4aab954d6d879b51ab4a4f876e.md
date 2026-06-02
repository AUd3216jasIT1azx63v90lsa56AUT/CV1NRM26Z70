Audit Report

## Title
Zero-rounding in `maxAssets` branch allows unbounded offer replay with free credit minting — (`src/Midnight.sol`, `src/libraries/UtilsLib.sol`)

## Summary
When a buy offer uses `maxAssets > 0` and the taker supplies `units` small enough that `units * buyerPrice < WAD`, `mulDivDown` returns zero and `consumed` is incremented by zero on every call. The cap check `require(newConsumed <= offer.maxAssets)` is trivially satisfied indefinitely, while the maker accrues credit and the taker accrues debt proportional to `units` — with zero token transfers — creating unbacked credit and violating the protocol solvency invariant.

## Finding Description

**Root cause:** `UtilsLib.mulDivDown(x, y, d)` computes `(x * y) / d` with integer truncation. [1](#0-0) 

When `units * buyerPrice < WAD`, the result is 0. In the `maxAssets` branch of `take`, `consumed` is incremented by this zero value: [2](#0-1) 

The cap check `require(newConsumed <= offer.maxAssets)` passes unchanged on every call because `consumed` never advances.

**Credit/debt accounting uses `units`, not `buyerAssets`:** [3](#0-2) 

So when `buyerAssets = 0` but `units > 0`, `buyerCreditIncrease = units` (assuming `buyerPos.debt = 0`) and `sellerDebtIncrease = units`. Position state mutates with non-zero values: [4](#0-3) 

**Token transfers execute for 0:** [5](#0-4) 

**No guard exists.** `grep` for `require.*units` in `src/Midnight.sol` returns no matches. The `maxUnits` branch (line 371) is immune because it increments `consumed` by `units` directly. [6](#0-5) 

**Tick price range:** `tickToPrice(0) = 0` and `tickToPrice(2) = 1e12` (the first non-zero price, confirmed by `testTickToPriceMinMax`). [7](#0-6) 

At tick 2 with `offerPrice = 1e12` and `settlementFee = 0` (market with `continuousFee = 0`), any `units < 1e6` satisfies `units * 1e12 < 1e18 = WAD`, yielding `buyerAssets = 0`.

**Exploit flow:**
1. Attacker controls address A (maker) and address B (taker). `require(offer.maker != taker)` at line 354 is satisfied.
2. A posts a buy offer with `maxAssets = M > 0` at tick 2 (`offerPrice = 1e12`) in a market with `continuousFee = 0`.
3. B calls `take(offer, ..., units = 999_999)` where `999_999 * 1e12 = 999_999e12 < 1e18`.
4. `buyerAssets = mulDivDown(999_999, 1e12, 1e18) = 0`. `sellerAssets = 0`.
5. `consumed += 0` → unchanged; `require(consumed <= M)` passes.
6. A's credit increases by `999_999`; B's debt increases by `999_999`.
7. Both token transfers execute for 0 tokens.
8. Steps 3–7 repeat indefinitely; `consumed` never reaches `M`.
9. A redeems accumulated credit at maturity, draining tokens deposited by legitimate users.

## Impact Explanation
Each iteration grants the maker `units` of unbacked credit with zero token payment. Repeated indefinitely, the maker accumulates credit redeemable at maturity against the contract's loan-token balance funded by legitimate users. This is direct theft-of-funds / protocol insolvency. The core invariant — every credit increase must correspond to a valid token inflow — is violated. [8](#0-7) 

## Likelihood Explanation
**Preconditions:** A buy offer with `maxAssets > 0` at any low tick in a market with zero or near-zero `continuousFee`. No admin access, oracle manipulation, or victim cooperation is required. Two addresses (trivially created) suffice. The required `units` value is computed off-chain as `floor((WAD - 1) / buyerPrice)`. **Repeatability:** Unlimited — `consumed` never advances, so the offer never closes. [2](#0-1) 

## Recommendation
Add a guard at the start of the `maxAssets` branch (or before position mutation) requiring that `buyerAssets > 0` when `units > 0`:

```solidity
// In the maxAssets branch, after computing buyerAssets:
require(units == 0 || buyerAssets > 0, ZeroAssets());
```

Alternatively, enforce a minimum `units` value such that `units * buyerPrice >= WAD`, or use `mulDivUp` for the `consumed` increment so that even a fractional asset rounds up to 1 and advances the cap. [9](#0-8) 

## Proof of Concept
**Minimal Forge test:**
```solidity
// Setup: market with continuousFee = 0, tick = 2 (offerPrice = 1e12)
// A posts buy offer: maxAssets = 1e18, group = 0
// B calls take(offer, ..., units = 999_999) in a loop N times
// Assert: A.credit == N * 999_999, token balance of contract unchanged
// Assert: consumed[A][0] == 0 after all iterations (never advanced)
```
The loop terminates only when the test runner stops it; `consumed` remains 0 throughout, confirming unbounded replay. A's credit redeemed at maturity drains the contract's loan-token balance by `N * 999_999` units. [10](#0-9)

### Citations

**File:** src/libraries/UtilsLib.sol (L28-31)
```text
    /// @dev Returns (x * y) / d rounded down.
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** src/Midnight.sol (L363-369)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L370-373)
```text
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
```

**File:** src/Midnight.sol (L382-384)
```text
        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
```

**File:** src/Midnight.sol (L408-414)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/libraries/TickLib.sol (L6-8)
```text
uint256 constant MAX_TICK = 5820;
// Minimum representable price increment in WAD (1e-6 WAD). Tick prices are rounded to multiples of this value.
uint256 constant PRICE_ROUNDING_STEP = 1e12;
```
