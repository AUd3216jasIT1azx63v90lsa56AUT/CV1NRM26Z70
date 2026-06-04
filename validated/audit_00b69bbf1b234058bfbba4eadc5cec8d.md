### Title
Inclusive Maturity Boundary in `take()` Allows Debt Increase at Exactly `block.timestamp == maturity` — (File: src/Midnight.sol)

---

### Summary

`take()` guards against post-maturity debt creation with `block.timestamp <= offer.market.maturity` (line 391), an inclusive `<=` comparison. This permits a seller to increase debt at the exact block where `block.timestamp == market.maturity`. The protocol's own invariant documentation explicitly forbids this case by name ("timestamp equality"), and the `liquidate()` function uses a strict `>` for the same boundary, creating a structural inconsistency between the two checks.

---

### Finding Description

**Root cause — off-by-one in the maturity guard:** [1](#0-0) 

```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

The condition `block.timestamp <= offer.market.maturity` evaluates to `true` when `block.timestamp == market.maturity`, so a sell-side `take` that increases `sellerDebtIncrease > 0` is permitted at exactly the maturity timestamp.

**Inconsistency with `liquidate()`:** [2](#0-1) 

```solidity
require(
    !liquidationLocked(id, borrower)
        && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
    NotLiquidatable()
);
```

`liquidate()` uses strict `>` for post-maturity mode. At exactly `block.timestamp == market.maturity`:
- `take()` **allows** debt increase (`<=` is satisfied)
- `liquidate()` post-maturity mode is **not available** (`>` is not satisfied)

**Protocol invariant explicitly violated:**

The `live_context.json` core invariants state:

> "maturity boundary must not allow debt increase by rounding, **timestamp equality**, or callback sequencing" [3](#0-2) 

And the whitepaper constants confirm:

> "debt_increase_after_maturity_forbidden": true [4](#0-3) 

The word "timestamp equality" is a direct reference to the `block.timestamp == maturity` case that the current `<=` check fails to block.

---

### Impact Explanation

At the exact maturity block:

1. A seller executes `take()` with `sellerDebtIncrease > 0`. The guard passes because `maturity <= maturity`.
2. New debt is minted at maturity — the protocol's stated invariant is broken.
3. Post-maturity liquidation (`postMaturityMode = true`) is unavailable at this block because `liquidate()` requires `block.timestamp > maturity`.
4. Normal-mode liquidation requires `originalDebt > maxDebt` (unhealthy). If the borrower is healthy at maturity, no liquidation path is available for that block.
5. In the next block, post-maturity liquidation becomes available, but the LIF ramps from `WAD` (1.0) — no liquidation incentive exists immediately. [5](#0-4) 

The borrower gains a one-block window of debt that is simultaneously "overdue" (maturity has passed) yet not subject to post-maturity liquidation. Combined with the LIF starting at 1.0 at `maturity + 1`, this creates a window where the debt is effectively unliquidatable at no incentive cost to the attacker.

---

### Likelihood Explanation

- **No privileged access required.** Any unprivileged taker can execute this by submitting a sell-side `take` in the exact block where `block.timestamp == market.maturity`.
- **Timing is attacker-controllable.** Block timestamps on EVM chains are known in advance (within the slot). An attacker can target the maturity block deterministically.
- **Offer availability is not a barrier.** The attacker can be the maker of their own sell offer (via an authorized ratifier) and take it themselves — `offer.maker != taker` is enforced, but the attacker can use two controlled addresses.
- **Realistic on any EVM chain** where block timestamps align with the market's maturity value, which is a common case for round-number maturities.

---

### Recommendation

Change the maturity guard in `take()` from inclusive `<=` to strict `<`:

```solidity
// Before (vulnerable):
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());

// After (fixed):
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

This aligns with:
- The protocol's stated invariant ("timestamp equality" must not allow debt increase)
- The `liquidate()` boundary (`block.timestamp > market.maturity` for post-maturity mode), which now becomes the complement of the debt-increase guard [1](#0-0) 

---

### Proof of Concept

```solidity
// Foundry test sketch
function test_debtIncrease_atExactMaturity() public {
    // 1. Create market with maturity = T
    uint256 T = block.timestamp + 1 days;
    Market memory market = _createMarket(T);

    // 2. Maker (attacker-controlled address A) signs a sell offer
    Offer memory offer = Offer({
        market: market,
        buy: false,          // sell offer → seller increases debt
        tick: SOME_TICK,
        maxUnits: 1000e18,
        start: 0,
        expiry: T,           // expires at maturity
        maker: addrA,
        ...
    });

    // 3. Warp to exactly maturity
    vm.warp(T);

    // 4. Taker (attacker-controlled address B) takes the sell offer
    // require(block.timestamp <= offer.market.maturity) → T <= T → passes
    vm.prank(addrB);
    midnight.take(offer, ratifierData, 1000e18, addrB, addrB, address(0), "");

    // 5. Debt was increased at maturity — invariant violated
    assertGt(midnight.debtOf(id, addrA), 0);

    // 6. Post-maturity liquidation is NOT available at this block
    vm.expectRevert(NotLiquidatable.selector);
    midnight.liquidate(market, 0, 0, 1000e18, addrA, true, addrB, address(0), "");
}
```

Steps 4–6 demonstrate that debt is created at exactly maturity while post-maturity liquidation is simultaneously unavailable, confirming the boundary inconsistency and invariant violation. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** src/Midnight.sol (L391-391)
```text
        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/Midnight.sol (L644-647)
```text
            uint256 _maxLif = market.collateralParams[collateralIndex].maxLif;
            uint256 lif = postMaturityMode
                ? UtilsLib.min(_maxLif, WAD + (_maxLif - WAD) * (block.timestamp - market.maturity) / TIME_TO_MAX_LIF)
                : _maxLif;
```

**File:** live_context.json (L86-88)
```json
      "debt_increase_after_maturity_forbidden": true,
      "post_maturity_trading_purpose": "facilitate unwinding when liquidations are unprofitable",
      "overdue_debt_after_maturity_is_liquidatable_even_if_healthy": true
```

**File:** live_context.json (L219-222)
```json
    "maturity": [
      "pre-maturity and post-maturity paths must not be composable to bypass debt repayment",
      "maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing",
      "post-maturity unwinding must not mint free credit or erase debt incorrectly"
```
