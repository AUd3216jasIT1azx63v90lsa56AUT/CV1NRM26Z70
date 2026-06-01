### Title
Zero-asset rounding bypass allows offer reuse after full consumption in assets mode - (`src/Midnight.sol`)

### Summary

When `offer.maxAssets > 0` and `offer.buy == true`, the consumed accounting in `take` adds `buyerAssets = units.mulDivDown(buyerPrice, WAD)` to the running total. Because `mulDivDown` rounds toward zero, a small `units` value with `buyerPrice < WAD` produces `buyerAssets = 0`. The guard `require(newConsumed <= offer.maxAssets)` then passes even when `consumed` is already exactly `maxAssets`, allowing the take to proceed and mutate position state (credit, debt, `totalUnits`) with zero asset transfer. The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` already reproduces and confirms this exact behavior.

### Finding Description

**Code path** — `src/Midnight.sol`, function `take`:

```
Line 363: uint256 buyerAssets = offer.buy
              ? units.mulDivDown(buyerPrice, WAD)   // rounds DOWN
              : units.mulDivUp(buyerPrice, WAD);

Lines 367-369:
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());   // <= not <
}
```

**Root cause** — Two compounding issues:
1. `mulDivDown` can return 0 for small `units` when `buyerPrice < WAD` (i.e., `offerPrice < WAD`, reachable via any tick below `WAD`).
2. The guard is `<=` not `<`, so adding 0 to an already-saturated `consumed == maxAssets` still satisfies the check.

**Attacker-controlled inputs** — `units`, `offer.tick` (controls `buyerPrice`). No privileged role required; any taker can call `take`.

**Exploit flow (3-step)**:

| Step | Call | `buyerAssets` added | `consumed` after |
|------|------|---------------------|-----------------|
| 1 | `take(offer, U1)` | `maxAssets - 1` | `maxAssets - 1` |
| 2 | `take(offer, U2)` where `mulDivDown(U2*buyerPrice,WAD)=1` | `1` | `maxAssets` |
| 3 | `take(offer, 1)` where `mulDivDown(1*buyerPrice,WAD)=0` | `0` | `maxAssets` ≤ `maxAssets` ✓ |

Step 3 passes the guard and executes the full position-mutation block (lines 382–417): `buyerPos.credit` increases, `sellerPos.debt` increases, `_marketState.totalUnits` increases — all with zero token transfer.

**Why existing checks fail** — There is no check that `buyerAssets > 0` when `units > 0`, and no pre-check that `consumed < maxAssets` (strict) before the addition. The `<=` guard is the only gate, and it is defeated by the zero-rounding. [1](#0-0) [2](#0-1) 

### Impact Explanation

A fully-consumed buy offer (consumed == maxAssets) can be taken an unbounded number of additional times by any taker who supplies `units` small enough that `mulDivDown(units * buyerPrice, WAD) == 0`. Each such take:
- Grants the maker (lender) free credit units.
- Burdens the taker (borrower) with debt units.
- Inflates `totalUnits` in the market state.
- Transfers zero loan tokens, so the maker pays nothing for the extra credit.

This directly violates the core invariant: *offers cannot be replayed or reused after full consumption*. [3](#0-2) 

### Likelihood Explanation

**Preconditions**:
- `offer.maxAssets > 0`, `offer.buy == true` (standard lender offer configuration).
- `offerPrice < WAD` — any tick strictly below the WAD tick satisfies this; the test uses `MAX_TICK - 16`.
- The offer must be filled to exactly `consumed == maxAssets` first (achievable by the attacker themselves in one or two prior takes).

**Feasibility**: All inputs are attacker-controlled. No oracle manipulation, no admin access, no token owner cooperation required. The condition `mulDivDown(1 * buyerPrice, WAD) == 0` holds whenever `buyerPrice < WAD`, which is the common case for any non-par tick. Repeatable indefinitely after the offer is fully consumed. [4](#0-3) 

### Recommendation

Add a strict pre-check before the consumed update, and/or require that the asset delta is non-zero when units are non-zero:

```solidity
// Option A: strict pre-check (cleanest)
if (offer.maxAssets > 0) {
    uint256 assetDelta = offer.buy ? buyerAssets : sellerAssets;
    require(assetDelta > 0 || units == 0, ZeroAssetTake());
    newConsumed = consumed[offer.maker][offer.group] += assetDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}

// Option B: strict less-than pre-check
if (offer.maxAssets > 0) {
    require(consumed[offer.maker][offer.group] < offer.maxAssets, ConsumedAssets());
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Option A also prevents the degenerate case where `units > 0` but produces zero economic effect, which is independently undesirable. [5](#0-4) 

### Proof of Concept

The existing test `testBugBuyMaxAssetsBypass` already proves the bug. A complete fuzz/invariant test plan:

```solidity
// Foundry fuzz test
function testFuzz_OfferReuseAfterFullConsumption(
    uint256 units1,
    uint256 units2,
    int256 tick  // tick such that tickToPrice(tick) < WAD
) public {
    // Setup: lenderOffer.buy=true, maxAssets=M, maxUnits=0
    // Precondition: choose tick so buyerPrice < WAD
    // Step 1+2: fill offer until consumed == maxAssets
    // Step 3: call take(offer, 1) where mulDivDown(1*buyerPrice,WAD)==0
    // Assertions:
    uint256 creditBefore = midnight.creditOf(id, lender);
    uint256 debtBefore   = midnight.debtOf(id, borrower);
    (uint256 ba,) = take(1, borrower, lenderOffer);
    assertEq(ba, 0, "buyerAssets must be 0");
    // BUG: these should NOT change after full consumption
    assertEq(midnight.creditOf(id, lender), creditBefore, "credit must not increase");
    assertEq(midnight.debtOf(id, borrower), debtBefore,   "debt must not increase");
}

// Invariant: consumed == maxAssets => no further non-zero-units take succeeds with state change
function invariant_fullyConsumedOfferNoStateChange() public {
    if (consumed[maker][group] >= offer.maxAssets && offer.maxAssets > 0) {
        uint256 creditBefore = midnight.creditOf(id, maker);
        try midnight.take(offer, ..., 1, ...) {
            assertEq(midnight.creditOf(id, maker), creditBefore);
        } catch {}
    }
}
```

Expected: without the fix, the fuzz test fails because `creditOf` and `debtOf` increase after a zero-buyerAssets take on a fully-consumed offer. [3](#0-2) [6](#0-5)

### Citations

**File:** src/Midnight.sol (L361-373)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
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

**File:** src/Midnight.sol (L382-417)
```text
        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
        uint128 buyerPendingFeeIncrease =
            UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
        uint128 sellerPendingFeeDecrease = sellerPos.credit > 0
            ? UtilsLib.toUint128(sellerPos.pendingFee.mulDivUp(sellerCreditDecrease, sellerPos.credit))
            : 0;

        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
        require(
            !offer.reduceOnly || (offer.buy ? buyerCreditIncrease == 0 : sellerDebtIncrease == 0),
            MakerCreditOrDebtIncreased()
        );

        require(
            offer.market.enterGate == address(0) || buyerCreditIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseCredit(buyer),
            BuyerGatedFromIncreasingCredit()
        );
        require(
            offer.market.enterGate == address(0) || sellerDebtIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseDebt(seller),
            SellerGatedFromIncreasingDebt()
        );

        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);

        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
```

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
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
