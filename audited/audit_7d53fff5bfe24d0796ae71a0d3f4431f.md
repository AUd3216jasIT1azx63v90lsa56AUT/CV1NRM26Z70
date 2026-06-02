Audit Report

## Title
Off-by-one at maturity boundary permits debt increase while blocking post-maturity liquidation simultaneously - (File: `src/Midnight.sol`)

## Summary
A boundary mismatch between the debt-increase guard in `take` (`<=`) and the post-maturity liquidation gate in `liquidate` (`>`) creates a one-block window at `block.timestamp == market.maturity` where a borrower can increase debt in an expired market while being immune to post-maturity liquidation. Since `touchMarket` imposes no lower bound on maturity, any unprivileged caller can create a market expiring at the current block, making this condition trivially reproducible.

## Finding Description
**Root cause — boundary mismatch between two independent checks:**

`src/Midnight.sol` line 391 (debt-increase guard in `take`):
```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
`<=` permits `sellerDebtIncrease > 0` when `block.timestamp == market.maturity`.

`src/Midnight.sol` lines 620–624 (liquidatability check in `liquidate`):
```solidity
require(
    !liquidationLocked(id, borrower)
        && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
    NotLiquidatable()
);
```
`>` rejects post-maturity liquidation when `block.timestamp == market.maturity`.

**Why existing checks fail:**
- `touchMarket` at line 758 only enforces `market.maturity <= block.timestamp + 100 * 365 days` with no lower bound, so any caller can create a market with `maturity = block.timestamp`.
- The `SellerIsLiquidatable` check at line 476 only blocks unhealthy sellers; a well-collateralised borrower passes it.
- Normal-mode liquidation (`postMaturityMode=false`) also fails for a healthy borrower since `originalDebt <= maxDebt`.

**Exploit flow:**
1. Attacker calls `touchMarket` with `market.maturity = block.timestamp`. Market is immediately at its maturity timestamp.
2. Attacker (as borrower) supplies collateral and calls `take` with `sellerDebtIncrease > 0`. Line 391 evaluates `block.timestamp <= market.maturity` → `true` → no revert. Debt is written.
3. In the same block, any liquidator calls `liquidate(..., postMaturityMode=true)`. Line 622 evaluates `block.timestamp > market.maturity` → `false` → reverts `NotLiquidatable`.
4. Normal-mode liquidation also fails because the borrower is healthy.

**Test confirmation:** `test/LiquidationTest.sol` lines 170–173 explicitly asserts this behaviour:
```solidity
// At exact maturity: still not available (only valid strictly after maturity).
vm.warp(market.maturity);
vm.expectRevert(IMidnight.NotLiquidatable.selector);
midnight.liquidate(market, 0, 0, 0, borrower, true, address(this), address(0), "");
```
This confirms the liquidation-side `>` is intentional, making the debt-side `<=` the defect.

## Impact Explanation
At `block.timestamp == market.maturity` a borrower can open or increase a debt position in a market that is, by the protocol's own definition, expired. The borrowed assets are received by the borrower. For that block the position is immune to post-maturity liquidation and, if healthy, immune to normal-mode liquidation as well. The debt persists into subsequent blocks where it becomes liquidatable, but the borrower has successfully borrowed against an expired market for one block without the protocol's maturity enforcement applying. This constitutes an unauthorized state change and a concrete integrity failure in the protocol's state-transition model.

## Likelihood Explanation
Requires no privilege: any address can call `touchMarket` with `maturity = block.timestamp`, and any address can be the borrower. The condition is reproducible deterministically on any block by setting `maturity` to the current `block.timestamp` at market creation time. It is repeatable across every block on every chain. The only constraint is that the borrower must be healthy at the moment of the `take`, which is the normal operating condition for any new borrow.

## Recommendation
Align the debt-increase guard with the liquidation gate by changing the `<=` to `<` on line 391:

```solidity
// Before
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());

// After
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

This ensures that at `block.timestamp == market.maturity`, debt increases are blocked, consistent with the liquidation gate which only permits post-maturity liquidation strictly after maturity (`>`).

## Proof of Concept
```solidity
function testOffByOneMaturityBoundary() public {
    // 1. Create market expiring at current block
    market.maturity = block.timestamp;
    touchMarket(market);

    // 2. Borrower takes with debt increase — succeeds (should fail)
    collateralize(market, borrower, units);
    take(market, borrower, units); // sellerDebtIncrease > 0, passes line 391

    // 3. Post-maturity liquidation in same block — reverts (should succeed)
    vm.expectRevert(IMidnight.NotLiquidatable.selector);
    midnight.liquidate(market, 0, 0, 0, borrower, true, address(this), address(0), "");

    // 4. One second later, liquidation succeeds — debt persisted through the gap
    vm.warp(block.timestamp + 1);
    midnight.liquidate(market, 0, 0, 0, borrower, true, address(this), address(0), "");
}
```