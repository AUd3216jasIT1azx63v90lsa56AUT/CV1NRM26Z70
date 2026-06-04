### Title
`pendingFee` user-favored rounding in `withdraw` allows lenders to drain continuous fee revenue via `multicall` — (File: `src/Midnight.sol`)

---

### Summary

The `withdraw` function computes `pendingFeeDecrease` using `mulDivUp` (rounding in favor of the user). By batching many 1-unit withdrawals inside a single `multicall` call — so no time elapses and no fee accrues between calls — a lender can reduce their entire `pendingFee` to zero without paying it. This lets the lender withdraw their full `credit` balance instead of `credit − pendingFee`, extracting the protocol's expected continuous-fee revenue from the shared `withdrawable` pool.

---

### Finding Description

**Root cause — `withdraw`, line 490:**

```solidity
pendingFeeDecrease = UtilsLib.toUint128(_position.pendingFee.mulDivUp(units, _position.credit));
``` [1](#0-0) 

`mulDivUp` returns `⌈pendingFee × units / credit⌉`. For a 1-unit withdrawal this equals `⌈pendingFee / credit⌉`. When `pendingFee < credit` (the normal case — the fee is a small fraction of credit), every single 1-unit withdrawal rounds up to **1**, even though the exact proportional reduction is `pendingFee / credit ≪ 1`. Each call therefore over-reduces `pendingFee` by up to `1 − pendingFee/credit` units.

The contract's own comment acknowledges the risk:

```
/// @dev pendingFee updates are rounded in favor of the user. It could lead to fees manipulations too.
``` [2](#0-1) 

**Why `multicall` makes it atomic:**

`_updatePosition` is called at the top of every `withdraw`. It accrues the fee proportional to elapsed time:

```solidity
uint128 fee = _lastAccrual < market.maturity
    ? uint128(postSlashPendingFee.mulDivDown(accrualEnd - _lastAccrual, market.maturity - _lastAccrual))
    : 0;
``` [3](#0-2) 

After the first withdrawal, `lastAccrual` is set to `block.timestamp`. Every subsequent withdrawal in the **same block** sees `accrualEnd − lastAccrual = 0`, so `fee = 0`. No real fee accrues between the batched calls. [4](#0-3) 

`multicall` uses `delegatecall`, so all calls share the same `block.timestamp`:

```solidity
function multicall(bytes[] calldata calls) external {
    for (uint256 i = 0; i < calls.length; i++) {
        (bool success, bytes memory returnData) = address(this).delegatecall(calls[i]);
``` [5](#0-4) 

**Exploit path:**

1. Lender holds `credit = C`, `pendingFee = F` (where `F ≪ C`).
2. Lender calls `multicall` with `F` calls to `withdraw(market, 1, lender, lender)`.
3. Each call: `_updatePosition` accrues 0 (same block); `pendingFeeDecrease = ⌈F_i / C_i⌉ = 1`; `pendingFee` decreases by 1; `credit` decreases by 1.
4. After `F` calls: `pendingFee = 0`, `credit = C − F`.
5. Lender calls `withdraw(market, C − F, lender, lender)`.
6. Total withdrawn = `C` instead of the correct `C − F`.
7. `continuousFeeCredit` never increases by `F`; the fee claimer's claimable balance is short by `F` units.

---

### Impact Explanation

The lender extracts `F = pendingFee` extra loan-token units from the `withdrawable` pool. These units were earmarked as future continuous-fee revenue for the protocol's fee claimer. The fee claimer's `continuousFeeCredit` is never credited, and `withdrawable` is depleted by the stolen amount, potentially preventing the fee claimer from claiming their full entitlement.

Maximum `pendingFee` per position: `credit × MAX_CONTINUOUS_FEE × timeToMaturity / WAD`. For a 1-year market at the maximum rate (1 % p.a.), `F ≈ 0.01 × C`. On a $1 M position this is $10,000 per exploit. [6](#0-5) 

---

### Likelihood Explanation

- **No privilege required.** Any lender with a non-zero `pendingFee` can execute this.
- **Fully atomic.** `multicall` bundles all withdrawals into one transaction; no timing dependency.
- **Profitable on L2s.** The number of `withdraw` calls needed equals `pendingFee` (in token units). For tokens with low decimals or on cheap chains (Arbitrum, Base, etc.) the gas cost is well below the fee avoided.
- **Repeatable.** A lender can re-enter the market, accumulate a new `pendingFee`, and repeat.

---

### Recommendation

Round `pendingFeeDecrease` **down** (in favor of the protocol) in both `withdraw` and the analogous `sellerPendingFeeDecrease` in `take`:

```solidity
// withdraw — line 490
pendingFeeDecrease = UtilsLib.toUint128(_position.pendingFee.mulDivDown(units, _position.credit));

// take — line 387-389
uint128 sellerPendingFeeDecrease = sellerPos.credit > 0
    ? UtilsLib.toUint128(sellerPos.pendingFee.mulDivDown(sellerCreditDecrease, sellerPos.credit))
    : 0;
```

Rounding down ensures the protocol retains at least the proportional share of the pending fee on every partial exit, eliminating the rounding-accumulation attack vector. [7](#0-6) 

---

### Proof of Concept

**Setup:** 1-year market, `continuousFee = MAX_CONTINUOUS_FEE`, lender buys 1 000 000 units of credit.

- `pendingFee` set at buy time (rounded down): `≈ 10 000` units.
- `credit = 1 000 000`, `pendingFee = 10 000`.

**Attack (single transaction via `multicall`):**

```
for i in range(10_000):
    withdraw(market, units=1, onBehalf=attacker, receiver=attacker)
withdraw(market, units=990_000, onBehalf=attacker, receiver=attacker)
```

**Trace of first 3 iterations (same block, `lastAccrual = block.timestamp` after iteration 1):**

| Call | `pendingFee` before | `pendingFeeDecrease` (mulDivUp) | `pendingFee` after | `credit` after |
|------|--------------------|---------------------------------|--------------------|----------------|
| 1    | 10 000             | ⌈10000/1000000⌉ = 1             | 9 999              | 999 999        |
| 2    | 9 999              | ⌈9999/999999⌉ = 1               | 9 998              | 999 998        |
| …    | …                  | 1                               | …                  | …              |
| 10000| 1                  | ⌈1/990001⌉ = 1                  | 0                  | 990 000        |

**Final withdraw:** 990 000 units. Total extracted = **1 000 000 units**.

**Expected (correct) behavior:** lender should only be able to withdraw `1 000 000 − 10 000 = 990 000` units net of fees. The attacker extracts **10 000 extra units** — the protocol's continuous fee revenue — at the cost of one multicall transaction. [8](#0-7)

### Citations

**File:** src/Midnight.sol (L115-115)
```text
/// @dev pendingFee updates are rounded in favor of the user. It could lead to fees manipulations too.
```

**File:** src/Midnight.sol (L211-213)
```text
    function multicall(bytes[] calldata calls) external {
        for (uint256 i = 0; i < calls.length; i++) {
            (bool success, bytes memory returnData) = address(this).delegatecall(calls[i]);
```

**File:** src/Midnight.sol (L387-389)
```text
        uint128 sellerPendingFeeDecrease = sellerPos.credit > 0
            ? UtilsLib.toUint128(sellerPos.pendingFee.mulDivUp(sellerCreditDecrease, sellerPos.credit))
            : 0;
```

**File:** src/Midnight.sol (L481-500)
```text
    function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        MarketState storage _marketState = marketState[id];
        _updatePosition(market, id, onBehalf);

        Position storage _position = position[id][onBehalf];
        uint128 pendingFeeDecrease;
        if (_position.credit > 0) {
            pendingFeeDecrease = UtilsLib.toUint128(_position.pendingFee.mulDivUp(units, _position.credit));
            _position.pendingFee -= pendingFeeDecrease;
        }
        _position.credit -= UtilsLib.toUint128(units);
        _marketState.withdrawable -= UtilsLib.toUint128(units);
        _marketState.totalUnits -= UtilsLib.toUint128(units);

        emit EventsLib.Withdraw(msg.sender, id, units, onBehalf, receiver, pendingFeeDecrease);

        SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
    }
```

**File:** src/Midnight.sol (L814-816)
```text
        uint128 fee = _lastAccrual < market.maturity
            ? uint128(postSlashPendingFee.mulDivDown(accrualEnd - _lastAccrual, market.maturity - _lastAccrual))
            : 0;
```

**File:** src/Midnight.sol (L845-845)
```text
        _position.lastAccrual = uint128(block.timestamp);
```

**File:** src/libraries/ConstantsLib.sol (L18-18)
```text
uint32 constant MAX_CONTINUOUS_FEE = uint32(uint256(0.01e18) / uint256(365 days));
```
