### Title
Fully-consumed assets-based buy offer with `buyerPrice < WAD` can be taken indefinitely with zero `buyerAssets` but non-zero position state changes - (`src/Midnight.sol`)

### Summary
When `offer.maxAssets > 0` and `offer.buy == true`, the consumed accounting increments by `buyerAssets` (not `units`). If `buyerPrice < WAD` (i.e., `offerPrice < WAD` after settlement fee), a taker can supply `units = 1` after the offer is fully consumed, producing `buyerAssets = mulDivDown(1 * buyerPrice, WAD) = 0`, so `newConsumed = maxAssets + 0 = maxAssets ≤ maxAssets` passes the guard. The position state mutations (credit, debt, `totalUnits`) still execute with `units = 1`, violating the invariant that a fully-consumed offer must not allow further fills. This is confirmed by the existing test `testBugBuyMaxAssetsBypass` and acknowledged in a protocol comment.

### Finding Description
**Code path:**

`src/Midnight.sol` lines 363–373:
```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // rounds DOWN
    : units.mulDivUp(buyerPrice, WAD);

uint256 newConsumed;
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

**Root cause:** The guard at line 369 checks `newConsumed <= offer.maxAssets`. When `buyerPrice < WAD` (tick set below the WAD threshold, e.g. `MAX_TICK - 16`), `mulDivDown(1 * buyerPrice, WAD)` truncates to `0`. Adding `0` to an already-maxed `consumed` value still satisfies `<= maxAssets`, so the guard passes. All subsequent position mutations at lines 408–417 execute unconditionally on `units`, not on `buyerAssets`.

**Attacker inputs:**
- `offer.buy = true`, `offer.maxAssets > 0`, `offer.maxUnits = 0`
- `offer.tick` chosen so that `buyerPrice < WAD` (any tick where `tickToPrice(tick) + settlementFee < WAD`)
- First call: `units = U` such that `mulDivDown(U * buyerPrice, WAD) == maxAssets` — fully consumes the offer
- Subsequent calls: `units = 1` — `buyerAssets = 0`, consumed unchanged, position mutated

**Why existing checks fail:**
- `require(newConsumed <= offer.maxAssets)` — passes because `maxAssets + 0 ≤ maxAssets`
- No check enforces `units == 0` when `consumed >= maxAssets`
- No check enforces `buyerAssets > 0` when `units > 0`

The protocol comment at `src/Midnight.sol` line 94 acknowledges this: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* The test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` line 858 explicitly demonstrates and names it a bug. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
After an offer reaches `consumed == maxAssets`, any unprivileged taker can call `take(offer, ..., units=1)` indefinitely. Each call:
- passes the `ConsumedAssets` guard (consumed does not increase)
- increases the buyer's (maker's) `credit` by `buyerCreditIncrease` derived from `units = 1`
- increases the seller's (taker's) `debt` by `sellerDebtIncrease` derived from `units = 1`
- increases `totalUnits` by `buyerCreditIncrease`
- transfers zero loan tokens (since `buyerAssets = sellerAssets = 0`)

The maker's credit grows without bound at zero cost to the taker, corrupting the credit/debt accounting invariant and inflating `totalUnits` without any corresponding asset backing. [4](#0-3) [5](#0-4) 

### Likelihood Explanation
**Preconditions:**
1. `offer.buy == true` and `offer.maxAssets > 0` — standard offer configuration
2. `buyerPrice < WAD` — requires `tickToPrice(offer.tick) + settlementFee < WAD`; achievable at any tick below the WAD threshold (e.g. `MAX_TICK - 16` in the test)
3. Offer must be fully consumed — attacker can self-consume by being the first taker, or wait for organic fills

All preconditions are attacker-reachable without any privileged access. The attack is repeatable in every block at negligible gas cost (no token transfers occur). Any taker who is not the maker (`offer.maker != taker`) can execute it.

### Recommendation
Add a guard that prevents a non-zero `units` take from proceeding when the assets-based consumed cap is already reached and the incremental asset contribution is zero:

```solidity
if (offer.maxAssets > 0) {
    uint256 assetDelta = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || assetDelta > 0, ConsumedAssets());
    newConsumed = consumed[offer.maker][offer.group] += assetDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, enforce that when `consumed[offer.maker][offer.group] >= offer.maxAssets`, `units` must be zero before any state mutation occurs. [6](#0-5) 

### Proof of Concept
The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` already serves as the PoC. A minimal Foundry unit test plan:

```solidity
function testFullyConsumedBuyOfferReusable() public {
    // Setup: buy offer with buyerPrice < WAD (tick below WAD threshold)
    lenderOffer.maxUnits = 0;
    lenderOffer.maxAssets = 1e18;
    lenderOffer.tick = MAX_TICK - 16; // buyerPrice < WAD

    // Step 1: fully consume the offer
    uint256 U = TakeAmountsLib.buyerAssetsToUnits(address(midnight), id, lenderOffer, 1e18);
    take(U, borrower, lenderOffer);
    assertEq(midnight.consumed(lender, lenderOffer.group), 1e18);

    // Step 2: take with units=1 on fully-consumed offer
    uint256 creditBefore = midnight.creditOf(id, lender);
    uint256 debtBefore   = midnight.debtOf(id, borrower);

    // Expected: revert with ConsumedAssets — ACTUAL: succeeds
    (uint256 buyerAssets,) = take(1, borrower, lenderOffer);

    assertEq(buyerAssets, 0);                                    // no assets transferred
    assertEq(midnight.consumed(lender, lenderOffer.group), 1e18); // consumed unchanged
    assertGt(midnight.creditOf(id, lender), creditBefore);       // BUG: credit grew
    assertGt(midnight.debtOf(id, borrower), debtBefore);         // BUG: debt grew
}
```

Expected assertion: `take(1, ...)` should revert with `ConsumedAssets`. Actual result: it succeeds and mutates position state, confirmed by `testBugBuyMaxAssetsBypass`. [3](#0-2) [1](#0-0)

### Citations

**File:** src/Midnight.sol (L93-94)
```text
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L363-373)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
```

**File:** src/Midnight.sol (L408-417)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);

        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
```

**File:** test/TakeTest.sol (L858-889)
```text
    function testBugBuyMaxAssetsBypass() public {
        deal(address(loanToken), lender, 0); // lender pays 0
        collateralize(market, borrower, 100);

        lenderOffer.maxUnits = 0;
        lenderOffer.maxAssets = 1;
        lenderOffer.tick = MAX_TICK - 16; // offerPrice < WAD

        // Fully consume the offer before the take.
        vm.prank(lender);
        midnight.setConsumed(lenderOffer.group, lenderOffer.maxAssets, lender);

        uint256 lenderCreditBefore = midnight.creditOf(id, lender);
        uint256 borrowerDebtBefore = midnight.debtOf(id, borrower);
        uint256 totalUnitsBefore = midnight.totalUnits(id);
        uint256 lenderBalBefore = loanToken.balanceOf(lender);
        uint256 borrowerBalBefore = loanToken.balanceOf(borrower);

        (uint256 buyerAssets, uint256 sellerAssets) = take(1, borrower, lenderOffer);

        assertEq(buyerAssets, 0);
        assertEq(sellerAssets, 0);

        // Nothing observable to the cap or token balances changed:
        assertEq(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets);
        assertEq(loanToken.balanceOf(lender), lenderBalBefore);
        assertEq(loanToken.balanceOf(borrower), borrowerBalBefore);
        // But position state strictly changed:
        assertGt(midnight.creditOf(id, lender), lenderCreditBefore);
        assertGt(midnight.debtOf(id, borrower), borrowerDebtBefore);
        assertGt(midnight.totalUnits(id), totalUnitsBefore);
    }
```
