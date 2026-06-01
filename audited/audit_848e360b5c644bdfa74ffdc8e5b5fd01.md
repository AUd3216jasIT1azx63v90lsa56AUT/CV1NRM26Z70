### Title
Buy-offer `consumed` counter frozen at sub-cap value when `buyerAssets` rounds to zero, enabling indefinite fills - (File: src/Midnight.sol)

### Summary

In `take`, when `offer.buy = true` and `offer.maxAssets > 0`, the `consumed` mapping is incremented by `buyerAssets`, which is computed as `units.mulDivDown(buyerPrice, WAD)`. When `buyerPrice < WAD` (low tick) and `units < WAD / buyerPrice`, this rounds to zero. Because the increment is zero, the cap check `newConsumed <= offer.maxAssets` always passes regardless of how many times the call is repeated, and position state (credit, debt, `totalUnits`) changes on every call without any token transfer.

### Finding Description

**Code path** — `src/Midnight.sol:363-373`:

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // rounds DOWN → can be 0
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());   // passes when increment == 0
}
```

**Root cause** — When `offer.buy = true` and `buyerPrice < WAD`, any `units` satisfying `units * buyerPrice < WAD` produces `buyerAssets = 0`. The `consumed` mapping is incremented by zero, so `newConsumed` equals the pre-call value. The guard `newConsumed <= offer.maxAssets` is trivially satisfied for any pre-call value ≤ `maxAssets`, including `maxAssets - 1`.

**Preconditions**:
- `offer.buy = true`, `offer.maxAssets > 0`
- `offer.tick` chosen so `tickToPrice(tick) < WAD` (any tick below the WAD-price tick; `MAX_TICK - 16` is sufficient per the existing test)
- `consumed[maker][group]` anywhere from 0 to `maxAssets` (the scenario in the question uses `maxAssets - 1`)

**Exploit flow**:
1. Attacker (taker) observes a buy offer with `maxAssets > 0` at a tick where `buyerPrice < WAD`.
2. Calls `take(offer, ..., units=1, ...)` where `1 * buyerPrice < WAD`.
3. `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`.
4. `consumed` is unchanged; `require(consumed <= maxAssets)` passes.
5. `buyerCreditIncrease` and `sellerDebtIncrease` are computed from `units = 1`, so the maker gains 1 unit of credit and the taker gains 1 unit of debt — with zero token transfer.
6. Steps 2–5 repeat indefinitely.

**Why existing checks fail** — The only guard is `require(newConsumed <= offer.maxAssets)`. There is no check that `units == 0 || assetsIncrement > 0`. The protocol's own comment at line 94 acknowledges this: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* The Certora spec `Consume.spec` rule `takeConsumedAtMaxUnchangedAssets` (line 88–96) uses `NONDET` summaries for `mulDivDown`/`mulDivUp` (lines 11–12), so the prover never models the zero-rounding case and the rule does not catch this.

### Impact Explanation

An unprivileged taker can call `take` an unbounded number of times against a buy offer whose `consumed` is anywhere from 0 to `maxAssets`. Each call with `units` in the rounding-zero range transfers zero tokens yet increments the buyer's credit and the seller's debt by `units`. The `maxAssets` cap is permanently bypassed: the offer is fillable indefinitely, violating the invariant that a fully-consumed (or near-cap) offer must not allow further non-trivial fills.

### Likelihood Explanation

The precondition `buyerPrice < WAD` is reachable on any market whose tick spacing allows ticks below the WAD-price tick. No privileged action is required. The attacker only needs to be a valid taker (not the maker). The attack is repeatable in a single transaction via `multicall`. The existing test `testBugBuyMaxAssetsBypass` (line 858) already demonstrates the identical behavior with `consumed = maxAssets` (fully consumed), confirming the path is live.

### Recommendation

Add a guard inside the `maxAssets` branch that rejects a non-zero `units` input that produces a zero asset increment:

```solidity
if (offer.maxAssets > 0) {
    uint256 assetsIncrement = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || assetsIncrement > 0, ZeroAssetsForNonZeroUnits());
    newConsumed = consumed[offer.maker][offer.group] += assetsIncrement;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

This preserves intentional no-op takes (`units == 0`, used for callback invocation per line 93) while closing the rounding bypass for non-trivial fills.

### Proof of Concept

```solidity
function testConsumedFrozenAtSubCap() public {
    // Buy offer: maker = lender, taker = borrower.
    lenderOffer.maxUnits = 0;
    lenderOffer.maxAssets = 10;
    lenderOffer.tick = MAX_TICK - 16; // buyerPrice < WAD

    // Pre-set consumed to maxAssets - 1 (one short of cap).
    vm.prank(lender);
    midnight.setConsumed(lenderOffer.group, lenderOffer.maxAssets - 1, lender);

    collateralize(market, borrower, 100);

    uint256 consumedBefore = midnight.consumed(lender, lenderOffer.group);
    uint256 debtBefore     = midnight.debtOf(id, borrower);

    // units=1, buyerPrice < WAD → buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0
    (uint256 buyerAssets,) = take(1, borrower, lenderOffer);

    // Assert: fill succeeded (did not revert) — BUG: should revert
    assertEq(buyerAssets, 0, "buyerAssets must be 0 for rounding to trigger");

    // Assert: consumed did NOT advance — cap is frozen
    assertEq(midnight.consumed(lender, lenderOffer.group), consumedBefore,
        "consumed must remain at maxAssets-1");

    // Assert: position state changed despite zero assets — unbounded fill
    assertGt(midnight.debtOf(id, borrower), debtBefore,
        "debt increased with zero asset payment");

    // Assert: can repeat indefinitely
    take(1, borrower, lenderOffer);
    take(1, borrower, lenderOffer);
    assertEq(midnight.consumed(lender, lenderOffer.group), consumedBefore,
        "consumed still frozen after repeated fills");
}
```

Expected (correct) behavior: every `take(1, ...)` call should revert with `ZeroAssetsForNonZeroUnits` (or equivalent). Actual behavior: all calls succeed, `consumed` stays at `maxAssets - 1`, and debt grows without bound. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** certora/specs/Consume.spec (L9-17)
```text
    // Summaries for complex internals irrelevant to consumed-mapping properties.
    function IdLib.toId(Midnight.Market memory, uint256, address) internal returns (bytes32) => NONDET;
    function UtilsLib.mulDivDown(uint256, uint256, uint256) internal returns (uint256) => NONDET;
    function UtilsLib.mulDivUp(uint256, uint256, uint256) internal returns (uint256) => NONDET;
    function UtilsLib.msb(uint128) internal returns (uint256) => NONDET;
    function TickLib.tickToPrice(uint256) internal returns (uint256) => NONDET;
    function TickLib.wExp(int256) internal returns (uint256) => NONDET;
    function isHealthy(Midnight.Market memory, bytes32, address) internal returns (bool) => NONDET;
    function settlementFee(bytes32, uint256) internal returns (uint256) => NONDET;
```

**File:** certora/specs/Consume.spec (L88-97)
```text
/// If consumed is already at or above maxAssets before a take in assets mode, it remains unchanged.
rule takeConsumedAtMaxUnchangedAssets(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxUnits == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert consumedBefore >= offer.maxAssets => consumed(offer.maker, offer.group) == consumedBefore;
}
```
