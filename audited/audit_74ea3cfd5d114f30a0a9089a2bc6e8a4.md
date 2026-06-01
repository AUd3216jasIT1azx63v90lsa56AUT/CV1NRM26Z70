### Title
Off-by-one `<=` in `CannotIncreaseDebtPostMaturity` allows debt creation at exact maturity timestamp - (`src/Midnight.sol`)

### Summary
The guard at `src/Midnight.sol:391` uses `block.timestamp <= offer.market.maturity`, which is `true` when `block.timestamp == market.maturity`, permitting `sellerDebtIncrease > 0` at the exact maturity second. The protocol's own invariant explicitly forbids this: "maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing." Every existing post-maturity test warps to `maturity + 1`, leaving the equality edge untested and unguarded.

### Finding Description

**Exact code path:**

`src/Midnight.sol:359` computes `timeToMaturity`:
```solidity
uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
```
When `block.timestamp == market.maturity`, this yields `0`.

`src/Midnight.sol:360` then calls `settlementFee(id, 0)`, which at `src/Midnight.sol:971` falls into the `timeToMaturity < 1 days` branch and returns `settlementFeeCbp0 * CBP` — the 0d post-maturity breakpoint.

`src/Midnight.sol:386` computes `buyerPendingFeeIncrease`:
```solidity
buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD)
```
With `timeToMaturity == 0`, this is `0` — no continuous fee is charged for new credit.

`src/Midnight.sol:384` computes `sellerDebtIncrease = units - sellerCreditDecrease`, which is `> 0` when the seller has no existing credit to offset.

`src/Midnight.sol:391` — the guard:
```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
At `block.timestamp == market.maturity`, the left side is `true`, so the `require` passes unconditionally regardless of `sellerDebtIncrease`.

`src/Midnight.sol:414` then writes the debt increase to storage:
```solidity
sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

`src/Midnight.sol:476` checks health *after* the debt increase:
```solidity
require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```
This only blocks an already-unhealthy seller; a seller with sufficient collateral passes.

**Attacker inputs:** An unprivileged taker calls `take()` with a sell offer (`offer.buy == false`) where the maker/seller has no existing credit, at a block where `block.timestamp == market.maturity`. The seller supplies enough collateral to remain healthy post-fill.

**Why existing checks fail:**
- The `CannotIncreaseDebtPostMaturity` guard uses `<=` instead of `<`.
- The `SellerIsLiquidatable` check fires after the debt is written and only enforces health, not the maturity boundary.
- The post-maturity liquidation check at `src/Midnight.sol:622` uses strict `block.timestamp > market.maturity`, so at exactly maturity, post-maturity liquidation mode is unavailable — the newly created debt cannot be liquidated via that path until the next block.

**Dual-boundary inconsistency:** At `block.timestamp == market.maturity`, the protocol simultaneously treats the timestamp as:
- **Pre-maturity** for the debt-increase guard (`<=` passes, debt allowed)
- **Post-maturity** for fee accounting (`timeToMaturity == 0`, 0d breakpoint, zero continuous fee accrual on new credit)

### Impact Explanation
Debt is created at the exact maturity second in violation of the core invariant "debt_increase_after_maturity_forbidden". The new debt accrues zero continuous fee (because `timeToMaturity == 0`), meaning the buyer's credit position carries no pending fee obligation despite being created at the protocol's terminal boundary. Post-maturity liquidation of this debt is unavailable until `block.timestamp > market.maturity` (next block), creating a window where the debt exists but cannot be liquidated via the post-maturity path. If collateral value falls in that window, the position may become bad debt before any liquidator can act under post-maturity mode.

### Likelihood Explanation
Any taker can target the exact maturity block. Ethereum block timestamps are miner/validator-influenced within ~12 seconds; on L2s (Arbitrum, Base) sequencers set timestamps with finer granularity, making exact-second targeting straightforward. The precondition (seller has collateral, no existing credit) is easily arranged. The attack is repeatable across any market whose maturity falls on a reachable block timestamp.

### Recommendation
Change the comparison from `<=` to `<` at `src/Midnight.sol:391`:

```solidity
// Before (buggy):
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());

// After (fixed):
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

This aligns the debt-increase guard with the post-maturity liquidation check (`block.timestamp > market.maturity` at `src/Midnight.sol:622`) and with the invariant that `timeToMaturity == 0` is a post-maturity state.

### Proof of Concept

```solidity
function testDebtIncreaseAtExactMaturity() public {
    // Setup: borrower (seller) has collateral, no existing credit
    uint256 units = 100;
    collateralize(market, borrower, units * 2); // healthy after fill

    // Warp to EXACTLY maturity (not maturity + 1)
    vm.warp(market.maturity);

    // Lender offer: lender is buyer (offer.buy == true), borrower is seller
    lenderOffer.expiry = market.maturity;
    lenderOffer.maxUnits = units;
    deal(address(loanToken), lender, units);

    uint256 debtBefore = midnight.debtOf(id, borrower);

    // This should revert with CannotIncreaseDebtPostMaturity but does NOT
    take(units, lender, lenderOffer);

    uint256 debtAfter = midnight.debtOf(id, borrower);

    // Assert: debt increased at maturity — invariant violated
    assertGt(debtAfter, debtBefore, "debt must not increase at maturity");

    // Assert: timeToMaturity was 0 (post-maturity fee regime applied)
    assertEq(midnight.settlementFee(id, 0), midnight.settlementFee(id, market.maturity - block.timestamp), "0d fee used");

    // Assert: post-maturity liquidation is NOT yet available at exact maturity
    vm.expectRevert(IMidnight.NotLiquidatable.selector);
    midnight.liquidate(market, 0, 0, units, borrower, true, borrower, address(0), "");
}
```

Expected assertions:
- Without the fix: `take` succeeds, `debtAfter > debtBefore` — invariant violated.
- With the fix (`<` instead of `<=`): `take` reverts with `CannotIncreaseDebtPostMaturity`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** src/Midnight.sol (L359-360)
```text
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
        uint256 _settlementFee = settlementFee(id, timeToMaturity);
```

**File:** src/Midnight.sol (L384-386)
```text
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
        uint128 buyerPendingFeeIncrease =
            UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
```

**File:** src/Midnight.sol (L391-391)
```text
        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

**File:** src/Midnight.sol (L476-476)
```text
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/Midnight.sol (L967-979)
```text
        if (timeToMaturity >= 360 days) return _marketState.settlementFeeCbp6 * CBP;

        // forgefmt: disable-start
        (uint256 start, uint256 end, uint256 feeLower, uint256 feeUpper) =
            timeToMaturity < 1 days   ? (  0 days,   1 days, _marketState.settlementFeeCbp0 * CBP, _marketState.settlementFeeCbp1 * CBP) :
            timeToMaturity < 7 days   ? (  1 days,   7 days, _marketState.settlementFeeCbp1 * CBP, _marketState.settlementFeeCbp2 * CBP) :
            timeToMaturity < 30 days  ? (  7 days,  30 days, _marketState.settlementFeeCbp2 * CBP, _marketState.settlementFeeCbp3 * CBP) :
            timeToMaturity < 90 days  ? ( 30 days,  90 days, _marketState.settlementFeeCbp3 * CBP, _marketState.settlementFeeCbp4 * CBP) :
            timeToMaturity < 180 days ? ( 90 days, 180 days, _marketState.settlementFeeCbp4 * CBP, _marketState.settlementFeeCbp5 * CBP) :
                                        (180 days, 360 days, _marketState.settlementFeeCbp5 * CBP, _marketState.settlementFeeCbp6 * CBP);
        // forgefmt: disable-end

        return (feeLower * (end - timeToMaturity) + feeUpper * (timeToMaturity - start)) / (end - start);
```

**File:** live_context.json (L219-223)
```json
    "maturity": [
      "pre-maturity and post-maturity paths must not be composable to bypass debt repayment",
      "maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing",
      "post-maturity unwinding must not mint free credit or erase debt incorrectly"
    ],
```

**File:** test/TakeTest.sol (L666-690)
```text
    function testBuyPastMaturity(uint256 timestamp) public {
        timestamp = bound(timestamp, market.maturity + 1, type(uint32).max);
        vm.warp(timestamp);
        borrowerOffer.expiry = timestamp;
        borrowerOffer.maxUnits = 100;
        borrowerOffer.tick = MAX_TICK;
        deal(address(loanToken), lender, 100);
        collateralize(market, borrower, 100);

        vm.expectRevert(IMidnight.CannotIncreaseDebtPostMaturity.selector);
        take(100, lender, borrowerOffer);
    }

    function testSellPastMaturity(uint256 timestamp) public {
        timestamp = bound(timestamp, market.maturity + 1, type(uint32).max);
        vm.warp(timestamp);
        lenderOffer.expiry = timestamp;
        lenderOffer.maxUnits = 100;
        lenderOffer.tick = MAX_TICK;
        deal(address(loanToken), lender, 100);
        collateralize(market, borrower, 100);

        vm.expectRevert(IMidnight.CannotIncreaseDebtPostMaturity.selector);
        take(100, borrower, lenderOffer);
    }
```
