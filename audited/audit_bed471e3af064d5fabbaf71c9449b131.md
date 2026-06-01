The two critical code paths are now clear. Let me analyze the exact boundary behavior.

**`take` line 391:**
```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
At `block.timestamp == T`: `T <= T` is `true` → debt increase is permitted.

**`liquidate` line 620–622:**
```solidity
(postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt)
```
At `block.timestamp == T`: `T > T` is `false` → post-maturity liquidation blocked.  
At `block.timestamp == T+1`: `T+1 > T` is `true` → post-maturity liquidation available.

The existing tests (`testBuy1PostMaturity`, `testSell1PostMaturity`) only warp to `maturity + 1`, never testing the exact boundary. The test `testLiquidatePostMaturityModeBeforeMaturity` (lines 170–173) explicitly confirms that at `block.timestamp == maturity`, post-maturity liquidation reverts — but no test blocks debt creation at that same timestamp.

The protocol's own stated invariant in `live_context.json` line 221 reads: *"maturity boundary must not allow debt increase by rounding, **timestamp equality**, or callback sequencing"* — directly naming this exact case.

---

### Title
Debt increase permitted at exact maturity timestamp via `<=` boundary — (`src/Midnight.sol`)

### Summary
The maturity guard in `Midnight.take` uses `block.timestamp <= offer.market.maturity`, which permits `sellerDebtIncrease > 0` when `block.timestamp == maturity`. This violates the protocol's explicit invariant that debt must not increase at or after maturity. Debt created at exactly `T` is immediately subject to post-maturity liquidation at `T+1`, even if the position is healthy.

### Finding Description
In `Midnight.take` at line 391:

```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
``` [1](#0-0) 

When `block.timestamp == offer.market.maturity` (call it `T`), the left side evaluates to `true`, so the require passes unconditionally regardless of `sellerDebtIncrease`. The seller's debt is then increased at lines 384 and 414:

```solidity
uint256 sellerDebtIncrease = units - sellerCreditDecrease;
...
sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
``` [2](#0-1) [3](#0-2) 

In `Midnight.liquidate` at line 622, post-maturity mode requires `block.timestamp > market.maturity` (strict):

```solidity
(postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt)
``` [4](#0-3) 

So at `T`, post-maturity liquidation is blocked. At `T+1`, it is available for any borrower with debt, regardless of health. A borrower who creates debt at exactly `T` has a position that is immediately liquidatable one second later via `postMaturityMode = true`, even if fully collateralized.

**Exploit flow:**
1. Attacker (borrower/seller) signs or obtains a sell offer with `offer.expiry >= T` and `offer.market.maturity = T`.
2. Attacker (or a cooperating validator) submits `take` in a block with `block.timestamp == T`. The check `T <= T` passes; `sellerDebtIncrease > 0` is written to state.
3. At `T+1`, any liquidator calls `liquidate(..., postMaturityMode=true)`. The check `T+1 > T` passes. The position is liquidatable even if healthy.
4. LIF at `T+1` is `WAD + (maxLif - WAD) * 1 / TIME_TO_MAX_LIF` ≈ 1 (minimal incentive), but grows to `maxLif` over 15 minutes, at which point the liquidator can seize collateral at full incentive from a position that was healthy when created.

The protocol's own invariant document explicitly names this case: [5](#0-4) 

Existing post-maturity tests only test at `maturity + 1`, leaving the `== maturity` boundary untested for debt creation: [6](#0-5) 

### Impact Explanation
Debt is created at the maturity boundary in violation of the stated invariant. The resulting position is immediately liquidatable at `T+1` via post-maturity mode regardless of health. If the borrower is the victim (e.g., a maker whose offer is taken at exactly `T` by a front-running taker), their collateral becomes seizable within 15 minutes at full `maxLif` incentive, constituting unauthorized collateral seizure from a position that should never have had debt created at that timestamp.

### Likelihood Explanation
Requires `block.timestamp == market.maturity` exactly. This is achievable by a validator/miner who controls block timestamp within the allowed drift, or by an attacker who monitors the mempool and submits a transaction timed to land in the maturity block. Market maturities are public on-chain parameters, making the target timestamp known in advance. The condition is repeatable for any market whose maturity falls on a future block.

### Recommendation
Change the comparison in `Midnight.take` line 391 from `<=` to `<`:

```solidity
// Before
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());

// After
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
``` [1](#0-0) 

This aligns the debt-creation guard with the liquidation guard (which already uses strict `>`), closing the one-second window.

### Proof of Concept

```solidity
function testDebtCreationAtExactMaturity() public {
    uint256 units = 100;
    // Warp to exactly maturity (not maturity+1)
    vm.warp(market.maturity);
    borrowerOffer.expiry = market.maturity;
    borrowerOffer.maxUnits = units;
    deal(address(loanToken), lender, units);
    collateralize(market, borrower, units);

    // Should revert with CannotIncreaseDebtPostMaturity, but currently succeeds
    take(units, lender, borrowerOffer);

    // Debt was created at maturity — invariant violated
    assertGt(midnight.debtOf(id, borrower), 0, "debt created at maturity");

    // One second later: position is liquidatable via post-maturity mode
    vm.warp(market.maturity + 1);
    // This succeeds even though position is healthy
    midnight.liquidate(market, 0, 0, 0, borrower, true, address(this), address(0), "");
}

// Fuzz variant
function testFuzzDebtCreationAtMaturityBoundary(uint256 ts) public {
    ts = bound(ts, market.maturity, market.maturity + 1);
    vm.warp(ts);
    borrowerOffer.expiry = ts;
    borrowerOffer.maxUnits = 100;
    deal(address(loanToken), lender, 100);
    collateralize(market, borrower, 100);

    if (ts >= market.maturity) {
        vm.expectRevert(IMidnight.CannotIncreaseDebtPostMaturity.selector);
    }
    take(100, lender, borrowerOffer);
    // Assert: sellerDebtIncrease == 0 whenever ts >= maturity
}
```

**Expected assertions:**
- `take` at `block.timestamp == maturity` with `sellerDebtIncrease > 0` must revert with `CannotIncreaseDebtPostMaturity`.
- After fix, fuzz over `[maturity-1, maturity, maturity+1]` confirms debt increase is only permitted at `maturity-1`.

### Citations

**File:** src/Midnight.sol (L384-384)
```text
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
```

**File:** src/Midnight.sol (L391-391)
```text
        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

**File:** src/Midnight.sol (L414-414)
```text
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** live_context.json (L221-221)
```json
      "maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing",
```

**File:** test/TakeTest.sol (L315-326)
```text
    function testBuy1PostMaturity() public {
        uint256 units = 100;
        uint256 timestamp = market.maturity + 1;
        vm.warp(timestamp);
        borrowerOffer.expiry = timestamp;
        borrowerOffer.maxUnits = units;
        deal(address(loanToken), lender, units);
        collateralize(market, borrower, units);

        vm.expectRevert(IMidnight.CannotIncreaseDebtPostMaturity.selector);
        take(units, lender, borrowerOffer);
    }
```
