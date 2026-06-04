### Title
Continuous Fee Accrual Rounds to Zero for Low-Decimal Loan Tokens with Frequent Interactions — (File: src/Midnight.sol)

---

### Summary

The `updatePositionView` function computes the per-interaction continuous fee as `pendingFee * elapsed / remaining_time`. When the loan token has 6 decimals (e.g., USDC) and the position is small, `pendingFee` is a small integer in 6-decimal units. For any interaction where `elapsed` is short relative to `remaining_time`, this division truncates to zero via `mulDivDown`. The protocol accrues zero fee for that interval, and if the lender withdraws before enough time accumulates, the protocol permanently loses the fee that should have been collected.

---

### Finding Description

**Root cause — `updatePositionView`, lines 814–816:**

```solidity
uint128 fee = _lastAccrual < market.maturity
    ? uint128(postSlashPendingFee.mulDivDown(accrualEnd - _lastAccrual, market.maturity - _lastAccrual))
    : 0;
```

`postSlashPendingFee` carries the same decimal precision as the loan token, because it is derived from `pendingFee`, which is set in `take()` at lines 385–386:

```solidity
uint128 buyerPendingFeeIncrease =
    UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
```

`buyerCreditIncrease` is denominated in loan-token units (6 decimals for USDC). `continuousFee * timeToMaturity / WAD` is a dimensionless ratio ≤ 1, so `pendingFee` inherits the 6-decimal magnitude of `credit`.

**Concrete arithmetic for a 1 USDC position, 30-day market, max fee:**

| Variable | Value |
|---|---|
| `credit` | 1 USDC = 1 × 10⁶ |
| `MAX_CONTINUOUS_FEE` | `0.01e18 / 365 days` ≈ 317,097,919 |
| `timeToMaturity` | 30 days = 2,592,000 s |
| `pendingFee` | 1e6 × 317,097,919 × 2,592,000 / 1e18 ≈ **822** |

Fee accrual per second:
```
fee = 822 * 1 / 2,592,000 = 0  (mulDivDown truncates)
```

The fee is zero for any elapsed interval shorter than `2,592,000 / 822 ≈ 3,153 seconds (~52 minutes)`.

**Exploit path:**
1. Lender takes a position: `credit = 1e6`, `pendingFee = 822`.
2. Any protocol interaction (another `take`, `repay`, `updatePosition`) within 52 minutes triggers `_updatePosition` → `updatePositionView` → `fee = 0`.
3. `_position.lastAccrual` is updated to `block.timestamp` (line 845), consuming the elapsed window with zero fee collected.
4. Lender calls `withdraw` after 1 day of frequent interactions. `_updatePosition` is called one final time; because `pendingFee` is still 822 and `remaining ≈ 29 days`, the fee again rounds to 0.
5. Lender exits paying **0** continuous fee; expected fee for 1 day ≈ `822 × 86400 / 2592000 ≈ 27 units`.

The `continuousFeeCredit` balance (line 846) never increases for these intervals, so the protocol permanently loses the revenue.

---

### Impact Explanation

The protocol's continuous fee mechanism silently fails for any USDC/USDT (6-decimal) market where:
- Individual positions are small (≤ a few USDC), **or**
- Interactions occur more frequently than the rounding threshold (~52 min for 1 USDC at max fee).

A lender can hold a credit position for its full duration and pay zero continuous fee by ensuring at least one protocol interaction occurs within every 52-minute window. The `continuousFeeCredit` accrual is permanently lost — it cannot be recovered retroactively. At scale (many small positions or a bot-driven lender), this represents a systematic drain of protocol fee revenue.

---

### Likelihood Explanation

- USDC and USDT are the most common loan tokens in fixed-rate lending protocols; 6-decimal markets are the default, not an edge case.
- `_updatePosition` is called automatically inside `take()` for both buyer and seller (lines 379–380), meaning every trade in the market resets `lastAccrual` for active lenders — no deliberate spam is required.
- A lender with a modest position who participates in a liquid market (frequent trades) will naturally trigger this rounding on every interaction without any adversarial intent.
- The threshold scales inversely with position size: a 10 USDC position has a ~5-minute threshold; a 100 USDC position has a ~30-second threshold. Any active market will cross these thresholds routinely.

---

### Recommendation

Scale `pendingFee` to a higher-precision intermediate before the time-weighted division, then scale back. For example, multiply by a precision factor (e.g., `1e12`) before dividing, then divide by the same factor after:

```solidity
uint128 fee = _lastAccrual < market.maturity
    ? uint128(
        postSlashPendingFee.mulDivDown(
            (accrualEnd - _lastAccrual) * PRECISION_SCALE,
            market.maturity - _lastAccrual
        ) / PRECISION_SCALE
      )
    : 0;
```

Alternatively, accumulate fees in a higher-precision internal unit (analogous to how Morpho Blue uses `shares` to avoid precision loss) and convert to token units only at claim time.

---

### Proof of Concept

**Setup:**
- Loan token: USDC (6 decimals)
- `continuousFee` = `MAX_CONTINUOUS_FEE` ≈ 317,097,919
- Market maturity: 30 days from now
- Lender takes position: `credit = 1e6` (1 USDC), `pendingFee = 822`

**Step-by-step:**

```
t=0:       take() → _updatePosition → lastAccrual = 0, pendingFee = 822
t=60s:     take() → _updatePosition
           fee = 822 * 60 / 2,592,000 = 0  ← rounds to zero
           lastAccrual = 60, pendingFee still 822
t=120s:    take() → _updatePosition
           fee = 822 * 60 / 2,591,940 = 0  ← rounds to zero
           ...repeated every 60 seconds for 86,400 seconds (1 day)...
t=86400s:  withdraw() → _updatePosition
           remaining = 2,592,000 - 86,400 = 2,505,600
           fee = 822 * 1 / 2,505,600 = 0  ← still zero
           Lender withdraws full credit, pays 0 continuous fee.
```

**Expected fee for 1 day** = `822 × 86,400 / 2,592,000 ≈ 27 units` (0.000027 USDC).
**Actual fee collected** = **0**.

The `continuousFeeCredit` in `MarketState` is never incremented for any of these intervals, confirming permanent protocol revenue loss. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** src/Midnight.sol (L385-386)
```text
        uint128 buyerPendingFeeIncrease =
            UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
```

**File:** src/Midnight.sol (L814-816)
```text
        uint128 fee = _lastAccrual < market.maturity
            ? uint128(postSlashPendingFee.mulDivDown(accrualEnd - _lastAccrual, market.maturity - _lastAccrual))
            : 0;
```

**File:** src/Midnight.sol (L844-846)
```text
        _position.pendingFee = newPendingFee;
        _position.lastAccrual = uint128(block.timestamp);
        marketState[id].continuousFeeCredit += UtilsLib.toUint128(accruedFee);
```

**File:** src/libraries/ConstantsLib.sol (L18-18)
```text
uint32 constant MAX_CONTINUOUS_FEE = uint32(uint256(0.01e18) / uint256(365 days));
```
