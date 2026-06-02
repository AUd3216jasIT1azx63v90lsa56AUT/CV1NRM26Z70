Audit Report

## Title
Zero-rounding in `maxAssets` branch enables unbounded offer replay with free credit minting and no token transfer — (`src/Midnight.sol`, `src/libraries/UtilsLib.sol`)

## Summary
When a buy offer has `maxAssets > 0` and the taker supplies `units` small enough that `units * buyerPrice < WAD`, `mulDivDown` returns zero and `consumed` is incremented by zero. The `require(newConsumed <= offer.maxAssets)` check passes unchanged on every call, so the offer is never exhausted. Meanwhile, the maker accrues credit and the taker accrues debt from `units` (not `buyerAssets`), with zero token transfers, creating unbacked credit and violating the protocol solvency invariant.

## Finding Description

**Root cause:** `mulDivDown(units, buyerPrice, WAD)` performs `(units * buyerPrice) / WAD`. When `units * buyerPrice < WAD`, the quotient is 0. The `maxAssets` branch increments `consumed` by this zero value, so the cap check is trivially satisfied on every call.

**Exact code path:**

`src/libraries/UtilsLib.sol` lines 29–31:
```solidity
function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y) / d;   // returns 0 when x*y < d
}
``` [1](#0-0) 

`src/Midnight.sol` line 363 — `buyerAssets` rounds to zero for small `units`: [2](#0-1) 

`src/Midnight.sol` lines 367–369 — `consumed += 0`, cap check passes unchanged: [3](#0-2) 

`src/Midnight.sol` lines 382–384 — `buyerCreditIncrease` and `sellerDebtIncrease` are computed from `units`, not `buyerAssets`, so they are non-zero even when `buyerAssets = 0`: [4](#0-3) 

`src/Midnight.sol` lines 408–414 — position state mutates with non-zero values: [5](#0-4) 

`src/Midnight.sol` lines 455–456 — both transfers execute for 0 tokens: [6](#0-5) 

**No `units > 0` or `buyerAssets > 0` guard exists.** `grep` for `require.*units` in `src/Midnight.sol` returns no matches. The only units-related require is `require(newConsumed <= offer.maxUnits)` in the `maxUnits` branch (line 372), which is immune because it increments by `units` directly. [7](#0-6) 

**Tick price range:** `PRICE_ROUNDING_STEP = 1e12` is the minimum representable price. At tick 0, `tickToPrice` produces a price near `1e12`. With `offerPrice = 1e12`, any `units < 1e6` satisfies `units * offerPrice < WAD = 1e18`. [8](#0-7) [9](#0-8) 

**Exploit flow:**
1. Attacker controls address A (maker) and address B (taker). `require(offer.maker != taker)` at line 354 is satisfied.
2. A posts a buy offer with `maxAssets = M > 0` at tick 0 (`offerPrice ≈ 1e12`).
3. B calls `take(offer, ..., units = 999_999)` where `999_999 * 1e12 < 1e18`.
4. `buyerAssets = mulDivDown(999_999, 1e12, 1e18) = 0`. `sellerAssets = 0`.
5. `consumed += 0` → unchanged; `require(consumed <= M)` passes.
6. A's credit increases by `999_999`; B's debt increases by `999_999`.
7. Both token transfers execute for 0 tokens.
8. Steps 3–7 repeat indefinitely; `consumed` never reaches `M`.
9. A redeems accumulated credit at maturity, draining tokens deposited by legitimate users.

**Why existing checks fail:**
- `require(newConsumed <= offer.maxAssets)` enforces an upper bound but does not require `consumed` to have increased.
- There is no `require(units == 0 || buyerAssets > 0)` guard.
- The `maxUnits` branch uses `+= units` and is immune; only the `maxAssets` branch is affected.

## Impact Explanation
Each iteration grants the maker `units` of credit with zero token payment and burdens the taker with `units` of debt with zero token receipt. Repeated indefinitely, the maker accumulates unbounded credit not backed by any token deposit. When the maker redeems this credit at maturity, the protocol pays out tokens deposited by legitimate users, directly draining the contract's loan-token balance. This is direct protocol insolvency / theft-of-funds. The `live_context.json` core invariants explicitly state: "contract token balances must cover withdrawable assets, collateral claims, credit redemptions, and accrued fees" and "every credit increase must correspond to valid debt creation, debt reduction, or settled repayment state" — both are violated. [10](#0-9) 

## Likelihood Explanation
**Preconditions:** A buy offer with `maxAssets > 0` must exist at any tick where `offerPrice < WAD` (true for all valid ticks, since prices are discount factors below 1). The attacker needs two addresses (trivially created). No admin access, oracle manipulation, or victim cooperation is required. The required `units` value is computed off-chain as `floor((WAD - 1) / offerPrice)`. **Repeatability:** Unlimited — `consumed` never advances, so the offer never closes.

## Recommendation
Add a guard at the start of the `maxAssets` branch (or before position mutation) requiring that the computed asset amount is non-zero when `units > 0`:

```solidity
uint256 assetAmount = offer.buy ? buyerAssets : sellerAssets;
if (offer.maxAssets > 0) {
    require(units == 0 || assetAmount > 0, ZeroAssetAmount());
    newConsumed = consumed[offer.maker][offer.group] += assetAmount;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, add a global `require(units > 0)` at the top of `take` and enforce a minimum `units` such that `units * minPrice >= WAD`, or enforce `require(buyerAssets > 0 || units == 0)` before position mutation.

## Proof of Concept
Minimal Foundry test:
```solidity
// Setup: market with loanToken, tick 0 (offerPrice ≈ 1e12)
// A posts buy offer: maxAssets = 1e18, maxUnits = 0, tick = 0
// B calls take(offer, ..., units = 999_999) repeatedly N times
// Assert: A.credit == N * 999_999, loanToken.balanceOf(address(midnight)) unchanged
// Assert: consumed[A][group] == 0 after all iterations (never advanced)
// Warp to maturity, A redeems credit → protocol pays from legitimate deposits
```

The invariant fuzz test axis `"0"` and `"1 wei"` under `recommended_fuzz_axes.amounts` in `live_context.json` directly covers this edge case. [11](#0-10)

### Citations

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** src/Midnight.sol (L363-363)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
```

**File:** src/Midnight.sol (L367-369)
```text
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

**File:** src/libraries/TickLib.sol (L8-8)
```text
uint256 constant PRICE_ROUNDING_STEP = 1e12;
```

**File:** src/libraries/TickLib.sol (L44-51)
```text
    function tickToPrice(uint256 tick) internal pure returns (uint256) {
        require(tick <= MAX_TICK, TickOutOfRange());
        unchecked {
            // forge-lint: disable-next-item(unsafe-typecast)
            return uint256(1e36)
                    .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
                    .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
        }
```

**File:** live_context.json (L187-198)
```json
    "solvency": [
      "contract token balances must cover withdrawable assets, collateral claims, credit redemptions, and accrued fees",
      "total claimable credit must not exceed repaid loan assets plus valid recoverable debt after loss accounting",
      "bad debt must reduce lender credit exactly once and proportionally"
    ],
    "credit_debt": [
      "every credit increase must correspond to valid debt creation, debt reduction, or settled repayment state",
      "every debt increase must pass collateral and gate checks",
      "buying units must reduce existing debt before increasing credit",
      "selling units must reduce existing credit before increasing debt",
      "debt must not increase after maturity"
    ],
```

**File:** live_context.json (L347-357)
```json
    "amounts": [
      "0",
      "1 wei",
      "dust - 1",
      "dust",
      "dust + 1",
      "max uint bounds where safe",
      "near offer remaining amount",
      "near debt amount",
      "near collateral threshold"
    ],
```
