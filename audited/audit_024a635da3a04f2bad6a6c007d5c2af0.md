### Title
Zero-increment consumed bypass allows unlimited takes on fully-consumed buy offers — (`src/Midnight.sol`)

### Summary

When `offer.buy == true` and `offer.maxAssets > 0`, the consumed accounting in `take` increments by `buyerAssets = units.mulDivDown(buyerPrice, WAD)`. If `units * buyerPrice < WAD`, `buyerAssets` rounds down to zero, so `newConsumed` does not advance. The `require(newConsumed <= offer.maxAssets)` guard then passes even when `consumed` is already at `maxAssets`, because `maxAssets + 0 <= maxAssets`. All downstream position-state mutations (credit, debt, `totalUnits`) still execute with the non-zero `units` value, creating real economic effects with zero asset transfer.

### Finding Description

**Code path — `src/Midnight.sol` lines 363–373:**

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
...
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
``` [1](#0-0) 

`mulDivDown` is integer division: `(units * buyerPrice) / WAD`. When `units * buyerPrice < WAD` (e.g., `units = 1` and `buyerPrice < WAD`, which holds for any `tick < MAX_TICK` with zero settlement fee), the result is `0`. [2](#0-1) 

**Exploit flow:**

1. Maker creates a buy offer: `offer.buy = true`, `offer.maxAssets = M`, `offer.tick` chosen so `buyerPrice < WAD` (any tick below `MAX_TICK`).
2. Taker fills normally until `consumed[maker][group] == M` (offer fully consumed).
3. Taker calls `take(offer, ..., units=1)`. Since `1 * buyerPrice < WAD`, `buyerAssets = 0`. `newConsumed = M + 0 = M <= M` — the guard passes.
4. Execution continues: `buyerCreditIncrease`, `sellerDebtIncrease`, `totalUnits` all update with `units = 1`. The maker (buyer) gains 1 unit of credit; the taker (seller) gains 1 unit of debt. Zero tokens are transferred (`buyerAssets - sellerAssets = 0`).
5. Step 3–4 can be repeated indefinitely.

**Why existing checks fail:**

- `require(newConsumed <= offer.maxAssets)` only catches the case where `buyerAssets > 0` pushes `newConsumed` over the cap. A zero-increment never triggers it.
- There is no `require(units == 0 || buyerAssets > 0)` guard.
- The `reduceOnly` check (line 392–395) only applies when `offer.reduceOnly` is set, which is not a precondition here. [3](#0-2) 

**Confirmed by existing test** `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol`: [4](#0-3) 

The test pre-sets `consumed = maxAssets = 1`, calls `take(1, borrower, lenderOffer)` with `tick = MAX_TICK - 16`, and asserts that `buyerAssets == 0`, `consumed` is unchanged at `maxAssets`, yet `creditOf(lender)`, `debtOf(borrower)`, and `totalUnits` all strictly increase.

### Impact Explanation

A fully-consumed buy offer (consumed == maxAssets) can be taken an unlimited number of times with `units > 0` as long as `units * buyerPrice < WAD`. Each such take:
- Increases the maker's credit and the taker's debt by `units` with zero token payment.
- Increases `totalUnits` and `claimableSettlementFee` by zero (no fee accrual), creating an accounting mismatch between units outstanding and assets backing them.
- Violates the core invariant: "offers cannot be replayed, overfilled, reused, or filled after cancel/deadline." [5](#0-4) 

### Likelihood Explanation

**Preconditions:**
- `offer.buy == true`, `offer.maxAssets > 0` — standard offer configuration.
- `buyerPrice < WAD` — holds for any tick below `MAX_TICK` with zero or low settlement fee; this is the common case.
- `units` chosen so `units * buyerPrice < WAD` — trivially satisfied with `units = 1` whenever `buyerPrice < WAD` (since `buyerPrice` is at most `WAD - 1` in that regime, so `1 * (WAD-1) < WAD`).

Any unprivileged taker can trigger this after any partial or full fill of a qualifying buy offer. It is repeatable without limit and requires no special permissions or oracle manipulation.

### Recommendation

Add a guard that prevents a non-zero `units` take from producing a zero asset increment when `maxAssets` mode is active:

```solidity
if (offer.maxAssets > 0) {
    uint256 assetsDelta = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || assetsDelta > 0, ZeroAssetIncrement());
    newConsumed = consumed[offer.maker][offer.group] += assetsDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

This ensures that any `units > 0` take must advance the consumed counter, making it impossible to reuse a fully-consumed offer via rounding.

### Proof of Concept

```solidity
// Foundry unit test
function testZeroIncrementBypass() public {
    // Setup: buy offer, maxAssets = 1, tick below MAX_TICK so buyerPrice < WAD
    lenderOffer.buy = true;
    lenderOffer.maxUnits = 0;
    lenderOffer.maxAssets = 1;
    lenderOffer.tick = MAX_TICK - 16; // buyerPrice < WAD

    // Fully consume the offer
    vm.prank(lender);
    midnight.setConsumed(lenderOffer.group, 1, lender);
    assertEq(midnight.consumed(lender, lenderOffer.group), 1);

    collateralize(market, borrower, 100);
    deal(address(loanToken), borrower, 0);

    uint256 creditBefore = midnight.creditOf(id, lender);
    uint256 debtBefore   = midnight.debtOf(id, borrower);

    // Take with units=1: buyerAssets rounds to 0, consumed stays at maxAssets
    (uint256 ba, uint256 sa) = take(1, borrower, lenderOffer);

    // Assertions that expose the bug:
    assertEq(ba, 0, "buyerAssets must be 0");
    assertEq(sa, 0, "sellerAssets must be 0");
    assertEq(midnight.consumed(lender, lenderOffer.group), 1, "consumed unchanged");
    // Bug: position state changed despite offer being fully consumed
    assertGt(midnight.creditOf(id, lender),  creditBefore, "lender credit increased");
    assertGt(midnight.debtOf(id, borrower),  debtBefore,   "borrower debt increased");

    // Fuzz extension: repeat N times, assert credit grows unboundedly
    for (uint i = 0; i < 100; i++) {
        take(1, borrower, lenderOffer);
    }
    assertGt(midnight.creditOf(id, lender), creditBefore + 100, "unbounded reuse");
}
```

**Expected assertions with the fix applied:** `take(1, borrower, lenderOffer)` reverts with `ZeroAssetIncrement` (or equivalent), and `consumed`, `creditOf`, `debtOf` remain unchanged.

### Citations

**File:** src/Midnight.sol (L363-369)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L391-395)
```text
        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
        require(
            !offer.reduceOnly || (offer.buy ? buyerCreditIncrease == 0 : sellerDebtIncrease == 0),
            MakerCreditOrDebtIncreased()
        );
```

**File:** src/Midnight.sol (L416-418)
```text
        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
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
