### Title
Fully-consumed buy offer with `buyerPrice < WAD` can be re-taken with non-zero units due to `mulDivDown` zero-rounding, bypassing the `maxAssets` cap - (`src/Midnight.sol`)

### Summary
When a buy offer uses `maxAssets` mode and `buyerPrice < WAD`, the expression `units.mulDivDown(buyerPrice, WAD)` floors to zero for sufficiently small `units`. Because `consumed` is incremented by `buyerAssets` (not by `units`), a zero `buyerAssets` result leaves `consumed` unchanged, allowing the cap check `newConsumed <= offer.maxAssets` to pass even when the offer is already fully consumed. An unprivileged taker can therefore repeatedly take non-zero units from a fully-consumed buy offer, accruing credit and debt for free with no token transfer.

### Finding Description
**Code path** — `src/Midnight.sol` lines 363–369:

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // floors toward zero
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

**Root cause** — For a buy offer, `consumed` is incremented by `buyerAssets`, not by `units`. When `buyerPrice < WAD`, `mulDivDown(1, buyerPrice, WAD) = 0` (integer floor). The cap check therefore passes with `newConsumed == consumed_before`, even when `consumed_before == maxAssets`.

**Differential behavior (the exploitable gap)**

| Price | `units = 1` → `buyerAssets` | Second take after full consume |
|---|---|---|
| `buyerPrice = WAD` | `1.mulDivDown(WAD, WAD) = 1` | `newConsumed = maxAssets + 1` → reverts ✓ |
| `buyerPrice < WAD` | `1.mulDivDown(buyerPrice, WAD) = 0` | `newConsumed = maxAssets + 0 = maxAssets` → passes ✗ |

**Exploit flow**

1. Attacker (taker) observes a buy offer with `maxAssets > 0` and `tick` such that `buyerPrice < WAD`.
2. Offer is fully consumed (either organically or via `setConsumed` by the maker).
3. Attacker calls `take(offer, ..., units=1, ...)` repeatedly.
4. Each call: `buyerAssets = 0`, `sellerAssets = 0`, `consumed` unchanged, cap check passes.
5. Each call still executes the full position-update path: `buyerCreditIncrease` and `sellerDebtIncrease` are computed from `units = 1`, so the buyer (maker/lender) gains credit and the seller (taker/borrower) gains debt — with zero token transfer.
6. The NatSpec at line 94 acknowledges this: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."*

**Why existing checks fail** — The only guard is `require(newConsumed <= offer.maxAssets)`. Because `consumed` is not incremented when `buyerAssets = 0`, this check is trivially satisfied. There is no check that `units == 0` when `buyerAssets == 0`, and no check that `consumed == maxAssets` implies a hard stop on non-zero `units`.

### Impact Explanation
A fully-consumed buy offer at any tick with `buyerPrice < WAD` can be taken indefinitely with `units = 1` (or any small value that rounds `buyerAssets` to zero). Each such take:
- Increases the maker's credit and the taker's debt by 1 unit with no asset transfer.
- Leaves `consumed` unchanged, so the cap is never enforced.
- Violates the core invariant: *offers cannot be overfilled or reused after the cap is reached*.

The maker's intended cap (`maxAssets`) is rendered meaningless for any price strictly below WAD, and the taker can manufacture unbounded debt/credit positions at zero cost.

### Likelihood Explanation
- **Preconditions**: buy offer with `maxAssets > 0`, any tick where `buyerPrice < WAD` (the vast majority of ticks), and `consumed >= maxAssets`.
- **Feasibility**: fully reachable by any unprivileged taker; no special role, oracle manipulation, or admin action required.
- **Repeatability**: the loop can be executed in a single transaction via `multicall`, bounded only by gas.

### Recommendation
Add an explicit guard that prevents non-zero `units` from being processed when `buyerAssets == 0` in assets-cap mode:

```solidity
if (offer.maxAssets > 0) {
    // Prevent zero-rounding bypass: if units > 0 but assets round to 0, treat as no-op or revert.
    require(units == 0 || (offer.buy ? buyerAssets : sellerAssets) > 0, ZeroAssetsNonZeroUnits());
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, track `consumed` in units (not assets) for the assets-cap mode and convert at check time, or enforce `require(units == 0)` whenever `consumed >= maxAssets` in assets mode.

### Proof of Concept
The existing test `testBugBuyMaxAssetsBypass()` in `test/TakeTest.sol` (lines 858–889) is a direct PoC. A differential fuzz test to confirm the gap:

```solidity
function testDifferentialConsumedAtPriceVsSubWAD(uint256 units) public {
    units = bound(units, 1, WAD - 1); // small enough to round to 0 at price < WAD

    // --- Case A: buyerPrice = WAD ---
    lenderOffer.maxAssets = units; // set cap = units so first fill exhausts it
    lenderOffer.tick = /* tick where buyerPrice == WAD */;
    take(units, borrower, lenderOffer); // consumed = units = maxAssets
    vm.expectRevert(IMidnight.ConsumedAssets.selector);
    take(1, borrower, lenderOffer);    // must revert

    // --- Case B: buyerPrice < WAD ---
    lenderOffer2.maxAssets = units;
    lenderOffer2.tick = MAX_TICK - 16; // buyerPrice < WAD
    // Pre-consume to maxAssets
    vm.prank(lender);
    midnight.setConsumed(lenderOffer2.group, units, lender);

    uint256 creditBefore = midnight.creditOf(id, lender);
    (uint256 ba,) = take(1, borrower, lenderOffer2); // must NOT revert
    assertEq(ba, 0, "buyerAssets must be zero");
    // consumed unchanged but credit increased — invariant violated
    assertEq(midnight.consumed(lender, lenderOffer2.group), units, "consumed unchanged");
    assertGt(midnight.creditOf(id, lender), creditBefore, "credit increased for free");
}
```

Expected assertions: Case A reverts; Case B succeeds with `buyerAssets = 0` and `creditOf` strictly greater than before, confirming the invariant breach. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** src/Midnight.sol (L93-94)
```text
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L363-369)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
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
