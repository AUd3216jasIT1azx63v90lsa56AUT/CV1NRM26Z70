### Title
Buy offer with `maxAssets` cap bypassed via zero-`buyerAssets` rounding when `buyerPrice < WAD` — (`src/Midnight.sol`)

### Summary

In `Midnight.take`, when a buy offer uses `maxAssets` as its cap, the consumed accounting increments by `buyerAssets = mulDivDown(units, buyerPrice, WAD)`. For any tick below `MAX_TICK`, `buyerPrice < WAD`, so small `units` values produce `buyerAssets = 0`. The guard `require(newConsumed <= offer.maxAssets)` then passes even when `consumed == maxAssets`, because `consumed` is not incremented. An unprivileged taker can repeatedly fill a fully-consumed buy offer, accruing unbounded credit/debt state changes at zero token cost.

### Finding Description

**Code path — `src/Midnight.sol` lines 363–369:**

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // rounds DOWN → can be 0
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());   // passes when delta == 0
}
```

**Root cause:** `consumed` is incremented by `buyerAssets`, not by `units`. When `buyerPrice < WAD` (every tick below `MAX_TICK`) and `units` is small enough that `floor(units * buyerPrice / WAD) == 0`, the storage slot is unchanged. The post-increment value equals the pre-increment value, so the `<= maxAssets` check is trivially satisfied regardless of whether the offer is already fully consumed.

**Attacker-controlled inputs:**
- `offer.tick` — any tick below `MAX_TICK` gives `buyerPrice < WAD`; tick = `MAX_TICK - 16` is used in the existing test
- `units = 1` — sufficient to produce `buyerAssets = 0` at low prices

**Exploit flow:**
1. Maker posts a buy offer with `maxAssets = M`, `tick < MAX_TICK`
2. Offer is fully consumed: `consumed[maker][group] == M`
3. Taker calls `take(units=1, ...)`:
   - `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`
   - `newConsumed = M + 0 = M`
   - `require(M <= M)` passes
   - `buyerPos.credit += 1`, `sellerPos.debt += 1`, `totalUnits += 1`
   - No tokens transferred (`buyerAssets == sellerAssets == 0`)
4. Step 3 can be repeated indefinitely; each iteration mints one unit of credit/debt at zero cost

**Why existing checks fail:** The only guard is `require(newConsumed <= offer.maxAssets)`. There is no check that `units == 0` or that `buyerAssets > 0` before mutating position state. The protocol's own NatSpec at line 94 acknowledges this: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* [1](#0-0) [2](#0-1) 

### Impact Explanation

A fully-consumed buy offer (consumed == maxAssets) continues to be fillable. Each fill with `units = 1` and `buyerAssets = 0`:
- Increases the maker's credit by 1 unit
- Increases the taker's debt by 1 unit
- Increases `totalUnits` by 1

No loan tokens are transferred in either direction. The attacker can inflate credit/debt accounting to arbitrary values, corrupting market-wide `totalUnits`, continuous-fee accrual, and health checks that depend on credit/debt balances — all without spending any assets. [3](#0-2) 

### Likelihood Explanation

**Preconditions:**
- Buy offer with `maxAssets > 0` and any `tick < MAX_TICK` (i.e., `buyerPrice < WAD = 1e18`) — this is the normal operating range; `MAX_TICK` is the only tick where price equals WAD
- Offer must be fully consumed (consumed == maxAssets); the attacker can reach this state by legitimately filling the offer, or by observing it on-chain

**Feasibility:** The condition `mulDivDown(1, buyerPrice, WAD) == 0` holds for every tick below `MAX_TICK` because `buyerPrice < 1e18`. No special oracle values or admin actions are required. The attack is permissionless, repeatable, and costs only gas. [4](#0-3) 

### Recommendation

Add an explicit guard that rejects fills where `units > 0` but the asset delta is zero, or alternatively check that `consumed` actually increased:

```solidity
if (offer.maxAssets > 0) {
    uint256 assetDelta = offer.buy ? buyerAssets : sellerAssets;
    require(assetDelta > 0 || units == 0, ZeroAssetDelta());   // new guard
    newConsumed = consumed[offer.maker][offer.group] += assetDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, add `require(units == 0 || buyerAssets > 0)` before the consumed block for buy offers in `maxAssets` mode. [5](#0-4) 

### Proof of Concept

The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` already reproduces the exact scenario and passes on the current codebase, confirming the bug is live:

```solidity
// test/TakeTest.sol — testBugBuyMaxAssetsBypass (lines 858–889)
lenderOffer.maxAssets = 1;
lenderOffer.tick = MAX_TICK - 16;          // buyerPrice < WAD

// Pre-consume to maxAssets
midnight.setConsumed(lenderOffer.group, 1, lender);

// Take with units=1 succeeds even though consumed == maxAssets
(uint256 buyerAssets, uint256 sellerAssets) = take(1, borrower, lenderOffer);

assertEq(buyerAssets, 0);                  // no tokens paid
assertEq(sellerAssets, 0);                 // no tokens received
assertEq(midnight.consumed(lender, ...), 1); // consumed unchanged at maxAssets
assertGt(midnight.creditOf(id, lender), lenderCreditBefore);   // FAIL: credit grew
assertGt(midnight.debtOf(id, borrower), borrowerDebtBefore);   // FAIL: debt grew
assertGt(midnight.totalUnits(id), totalUnitsBefore);           // FAIL: totalUnits grew
```

A complementary fuzz/invariant test should assert: for any buy offer with `maxAssets > 0`, if `consumed[maker][group] >= offer.maxAssets` before the call, then `take` with `units > 0` must revert. [6](#0-5)

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

**File:** test/TakeTest.sol (L857-889)
```text
    // Show that a buy offer with offerPrice < WAD can be taken with units > 0
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
