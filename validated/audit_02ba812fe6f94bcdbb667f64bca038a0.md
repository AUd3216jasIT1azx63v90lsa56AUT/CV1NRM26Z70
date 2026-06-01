Audit Report

## Title
`buyerPendingFeeIncrease` Truncates to Zero for Dust Fills, Enabling Continuous Fee Evasion - (File: `src/Midnight.sol`)

## Summary
In `Midnight.take`, `buyerPendingFeeIncrease` is computed via `mulDivDown` (floor division), which truncates to zero whenever `units * continuousFee * timeToMaturity < WAD`. A buyer can repeatedly call `take` with small `units` values to accumulate credit with `pendingFee = 0`, meaning no continuous fee is ever deducted from their position. This violates the protocol invariant that every credit increase on a market with `continuousFee > 0` and positive TTM must carry a proportional `pendingFee` obligation.

## Finding Description

**Root cause — `src/Midnight.sol` lines 385–386:**

```solidity
uint128 buyerPendingFeeIncrease =
    UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
```

`mulDivDown` performs `floor(x * y / d)`. When `buyerCreditIncrease * continuousFee * timeToMaturity < WAD = 1e18`, the result is zero.

**Concrete truncation conditions:**

`MAX_CONTINUOUS_FEE = uint32(0.01e18 / 365 days) ≈ 317,097,919` (from `src/libraries/ConstantsLib.sol` line 18).

- At `continuousFee = MAX_CONTINUOUS_FEE`, `timeToMaturity = 3153 s` (~52 min): `317,097,919 × 3153 ≈ 9.998e11 < 1e18` → `mulDivDown(1, 9.998e11, 1e18) = 0`.
- At `timeToMaturity = 1 s`: max zero-fee fill = `floor(1e18 / 317,097,919) - 1 = 3152` units per call.

**Exploit flow:**

1. Any market with `continuousFee > 0` and short TTM (or low fee) qualifies.
2. Taker calls `take(offer, ..., units=K)` where `K * continuousFee * timeToMaturity < WAD`.
3. `buyerCreditIncrease = K`, `buyerPendingFeeIncrease = 0` (truncated).
4. `buyerPos.credit += K`, `buyerPos.pendingFee += 0` (lines 409–410).
5. Repeat N times: buyer accumulates `N*K` credit with `pendingFee = 0`.
6. At any subsequent `_updatePosition`, accrued fee = `pendingFee.mulDivDown(elapsed, ttm) = 0` → zero credit deducted.

**Why existing checks fail:**

- No minimum `units` check in `take`.
- No `require(buyerPendingFeeIncrease > 0)` guard.
- The Certora rule `continuousFeeNotOverchargedForBuyer` (`certora/specs/ContinuousFee.spec` line 62) asserts `pendingFee == postUpdatePendingFee + floor(creditDelta * contFee * ttm / WAD)` — it only verifies the upper bound (no overcharge), not a lower bound.
- The invariant `pendingContinuousFeeBoundedByCredit` (`certora/specs/Midnight.spec` lines 137–149) only enforces `pendingFee ≤ credit`, not `pendingFee ≥ minExpectedFee`.

The protocol's own ROUNDINGS comment at line 115 acknowledges: *"pendingFee updates are rounded in favor of the user. It could lead to fees manipulations too."* — confirming this class of issue is known but no mitigation is implemented.

## Impact Explanation

A buyer systematically accumulates credit with `pendingFee = 0`. Since fee accrual is `pendingFee.mulDivDown(elapsed, ttm)`, zero `pendingFee` means zero fee deduction over the entire remaining TTM. The fee evaded per fill is up to just under 1 unit of credit; across many fills this is a repeatable, unbounded continuous-fee evasion. The protocol's fee revenue from continuous fees is directly reduced, and the accounting invariant linking credit to fee obligation is broken.

## Likelihood Explanation

No privileged access is required. Any unprivileged taker can trigger this on any market where `continuousFee > 0` and `timeToMaturity < WAD / continuousFee` (e.g., TTM < ~52 minutes at max fee). The condition becomes trivially satisfiable as any market approaches maturity. The attacker only needs to choose a small `units` value; no special setup beyond a valid offer is needed. On low-gas chains (L2s), the gas cost per fill is negligible relative to the accumulated fee evasion across many fills.

## Recommendation

Replace `mulDivDown` with `mulDivUp` for `buyerPendingFeeIncrease` so that any non-zero credit increase with non-zero `continuousFee` and positive TTM always results in at least 1 unit of `pendingFee`:

```solidity
uint128 buyerPendingFeeIncrease =
    UtilsLib.toUint128(buyerCreditIncrease.mulDivUp(_marketState.continuousFee * timeToMaturity, WAD));
```

Alternatively, add a guard: `if (buyerCreditIncrease > 0 && continuousFee > 0 && timeToMaturity > 0) require(buyerPendingFeeIncrease > 0)`. Also update the Certora `continuousFeeNotOverchargedForBuyer` rule to assert a lower bound (using `mulDivUp`) alongside the existing upper bound.

## Proof of Concept

```solidity
// Minimal forge test sketch
function test_dustFillZeroPendingFee() public {
    // Deploy market with continuousFee = MAX_CONTINUOUS_FEE, maturity = now + 3153s
    // Create buy offer with maxUnits = 3152 * N
    // Taker calls take() N times with units = 3152
    // Assert: buyerPos.pendingFee == 0 after all fills
    // Assert: buyerPos.credit == 3152 * N
    // Advance time to maturity, call updatePosition
    // Assert: credit unchanged (no fee deducted)
}
```

Concrete parameters: `continuousFee = 317_097_919`, `timeToMaturity = 1`, `units = 3152` per call. Each call produces `buyerCreditIncrease = 3152`, `buyerPendingFeeIncrease = floor(3152 * 317097919 * 1 / 1e18) = floor(9.994e11 / 1e18) = 0`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** src/Midnight.sol (L115-115)
```text
/// @dev pendingFee updates are rounded in favor of the user. It could lead to fees manipulations too.
```

**File:** src/Midnight.sol (L385-386)
```text
        uint128 buyerPendingFeeIncrease =
            UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
```

**File:** src/Midnight.sol (L409-410)
```text
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);
```

**File:** src/libraries/ConstantsLib.sol (L18-18)
```text
uint32 constant MAX_CONTINUOUS_FEE = uint32(uint256(0.01e18) / uint256(365 days));
```

**File:** certora/specs/ContinuousFee.spec (L62-62)
```text
    assert pendingFee(id, buyer) == postUpdatePendingFee + (creditDelta * contFee * timeToMaturity) / WAD();
```

**File:** certora/specs/Midnight.spec (L137-149)
```text
strong invariant pendingContinuousFeeBoundedByCredit(bytes32 id, address user)
    pendingFee(id, user) <= creditOf(id, user)
    {
        preserved with (env e) {
            requireInvariant continuousFeeBounded(id);
            requireInvariant defaultContinuousFeeBoundedAll();
        }
        preserved take(Midnight.Offer offer, bytes ratifierData, uint256 unitsInput, address taker, address receiverIfTakerIsSeller, address takerCallbackAddress, bytes takerCallbackData) with (env e) {
            requireInvariant continuousFeeBounded(id);
            requireInvariant defaultContinuousFeeBoundedAll();
            require to_mathint(offer.market.maturity) <= to_mathint(e.block.timestamp) + MAX_TTM(); // TODO verify this cleanly
        }
    }
```
