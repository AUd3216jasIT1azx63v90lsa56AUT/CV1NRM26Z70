The code at both cited lines is confirmed exactly as described in the report. Let me verify the key details:

**Line 391** (`src/Midnight.sol`): [1](#0-0) 

`block.timestamp <= offer.market.maturity` — at `T == T`, this is `true`, so the require passes unconditionally regardless of `sellerDebtIncrease`.

**Lines 620–622** (`src/Midnight.sol`): [2](#0-1) 

`postMaturityMode ? block.timestamp > market.maturity` — at `T+1`, `T+1 > T` is `true`, making any debt position liquidatable with no health check.

**`live_context.json` invariants** explicitly name this exact case: [3](#0-2) [4](#0-3) 

The asymmetry is real, the invariant violation is explicit, and the exploit path is reachable. This is a valid finding.

---

Audit Report

## Title
Debt increase permitted at exact maturity timestamp via `<=` boundary — (File: `src/Midnight.sol`)

## Summary
The maturity guard in `Midnight.take` at line 391 uses `block.timestamp <= offer.market.maturity`, permitting `sellerDebtIncrease > 0` when `block.timestamp == maturity`. The liquidation guard at line 622 uses the strict `block.timestamp > market.maturity` for post-maturity mode. Debt created at exactly `T` is immediately subject to unchecked post-maturity liquidation at `T+1`, violating the protocol's explicit invariant that "maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing" (`live_context.json` line 221).

## Finding Description
**Root cause — asymmetric boundary operators:**

`src/Midnight.sol` line 391:
```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
At `block.timestamp == T`, `T <= T` evaluates to `true`, so the require passes unconditionally regardless of `sellerDebtIncrease`. The debt increase computed at line 384 is written to state at line 414:
```solidity
uint256 sellerDebtIncrease = units - sellerCreditDecrease;
...
sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

`src/Midnight.sol` lines 620–622:
```solidity
require(
    !liquidationLocked(id, borrower)
        && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
    NotLiquidatable()
);
```
At `T`, `T > T` is `false` → post-maturity liquidation blocked. At `T+1`, `T+1 > T` is `true` → any borrower with debt is liquidatable regardless of health.

**Protocol invariants explicitly violated:**
- `live_context.json` line 221: `"maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing"` — names this exact case.
- `live_context.json` line 86: `"debt_increase_after_maturity_forbidden": true`
- `live_context.json` line 88: `"overdue_debt_after_maturity_is_liquidatable_even_if_healthy": true` — confirms that any debt existing post-maturity is unconditionally liquidatable, making the debt created at `T` immediately at risk.

**Exploit flow:**
1. Market with `maturity = T` is public on-chain. Attacker (or cooperating validator) targets the block with `block.timestamp == T`.
2. Attacker submits `take` with a sell offer where `sellerDebtIncrease > 0`. The check `T <= T` passes; debt is written to the seller's position.
3. At `T+1`, any liquidator calls `liquidate(..., postMaturityMode=true)`. The check `T+1 > T` passes. The position is liquidatable even if fully collateralized.
4. LIF grows from ~1 at `T+1` to `maxLif` over 15 minutes, at which point the liquidator seizes collateral at full incentive from a position that was healthy at creation.

If the victim is a maker (seller) whose offer is taken at exactly `T` by a front-running taker, the victim had no ability to prevent the take and their collateral becomes seizable within 15 minutes.

## Impact Explanation
Unauthorized collateral seizure from a healthy position. A borrower whose debt is created at exactly `T` is immediately subject to post-maturity liquidation at `T+1` with no health-based protection. This falls directly under the protocol's listed best bug classes: `"unauthorized collateral seizure"` and `"healthy-account liquidation"`. The borrower suffers a loss of collateral from a position that should never have had debt created at that timestamp per the protocol's own invariant.

## Likelihood Explanation
Requires `block.timestamp == market.maturity` exactly. Market maturities are public on-chain parameters, making the target timestamp known in advance. On proof-of-stake Ethereum, validators control block timestamps within the allowed drift window (~12 seconds), making exact timestamp targeting feasible for a validator-attacker. For non-validators, the condition requires a transaction landing in the exact maturity block, which is probabilistically achievable by monitoring the mempool and submitting with appropriate gas. The condition is repeatable across any market whose maturity falls on a future block.

## Recommendation
Change the boundary operator at line 391 from `<=` to `<`:
```solidity
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
This makes the debt-increase guard consistent with the liquidation guard: debt cannot be created at or after maturity (`>= T`), and post-maturity liquidation begins at `T+1` (`> T`). The one-second window at exactly `T` is closed, eliminating the asymmetry.

## Proof of Concept
Foundry test plan:
```solidity
function test_debtAtMaturityImmediatelyLiquidatable() public {
    // 1. Create market with maturity = T
    uint256 T = block.timestamp + 1 days;
    Market memory market = _createMarket(T);

    // 2. Seller posts a sell offer; buyer (attacker) prepares to take it
    Offer memory sellOffer = _makeSellOffer(market, seller);

    // 3. Warp to exactly T
    vm.warp(T);

    // 4. Taker takes the offer, causing sellerDebtIncrease > 0
    // Expect: require(T <= T || ...) passes, debt is written
    vm.prank(attacker);
    midnight.take(sellOffer, ...);

    // 5. Assert seller has debt
    assertGt(midnight.position(id, seller).debt, 0);

    // 6. Warp to T+1
    vm.warp(T + 1);

    // 7. Liquidator calls liquidate with postMaturityMode=true
    // Expect: block.timestamp > market.maturity → T+1 > T → true → liquidatable
    vm.prank(liquidator);
    midnight.liquidate(market, ..., seller, true, ...);

    // 8. Assert seller's collateral was seized despite position being healthy at creation
    assertLt(midnight.position(id, seller).collateral[0], initialCollateral);
}
```

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

**File:** live_context.json (L86-88)
```json
      "debt_increase_after_maturity_forbidden": true,
      "post_maturity_trading_purpose": "facilitate unwinding when liquidations are unprofitable",
      "overdue_debt_after_maturity_is_liquidatable_even_if_healthy": true
```

**File:** live_context.json (L221-221)
```json
      "maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing",
```
