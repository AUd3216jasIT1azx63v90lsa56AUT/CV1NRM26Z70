All code references check out. Here is the validation result:

---

Audit Report

## Title
Seller Self-Activates Reverting-Oracle Collateral to DoS All Takers on Their Offers - (File: `src/Midnight.sol`)

## Summary
A seller with existing debt can call `supplyCollateral` on their own position to activate a collateral index whose oracle reverts. Because `isHealthy` iterates every bit in the seller's `collateralBitmap` and calls `IOracle.price()` with no `try/catch`, a single reverting oracle causes `isHealthy` to revert. The unconditional `require` at `Midnight.sol:476` propagates this revert through every subsequent `take()` against any of that seller's offers, freezing taker-side liquidity for as long as the seller carries debt.

## Finding Description

**`isHealthy` (`Midnight.sol:944-960`)** iterates the seller's `collateralBitmap` with a bare external call and no error handling:

```solidity
while (_collateralBitmap != 0) {
    uint256 i = UtilsLib.msb(_collateralBitmap);
    CollateralParams memory collateralParam = market.collateralParams[i];
    uint256 price = IOracle(collateralParam.oracle).price(); // no try/catch
    ...
    _collateralBitmap = _collateralBitmap.clearBit(i);
}
```

**`take()` (`Midnight.sol:475-476`)** unconditionally reaches `isHealthy` in the normal (non-reentrant) path:

```solidity
if (!wasLocked) UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, false);
require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

`wasLocked` is set at line 444 via `tExchange(..., true)` and returns the *previous* value. In a normal external call, the previous value is `false`, so `wasLocked = false`. Line 475 then clears the lock, making `liquidationLocked` return `false`, so `isHealthy` is always called. The `liquidationLocked` short-circuit only fires in a reentrant `take` inside a callback.

**`supplyCollateral` (`Midnight.sol:523-546`)** sets a bit in `collateralBitmap` when `oldCollateral == 0 && assets > 0`. The authorization check (`onBehalf == msg.sender`) blocks third parties but explicitly permits the seller to call it on themselves:

```solidity
require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
...
if (oldCollateral == 0 && assets > 0) {
    uint128 newCollateralBitmap = _position.collateralBitmap.setBit(collateralIndex);
    _position.collateralBitmap = newCollateralBitmap;
    ...
}
```

There is no check that the oracle for the newly activated index is currently non-reverting.

**Exploit flow:**
1. Seller acquires debt via a prior `take`.
2. Seller calls `supplyCollateral(market, j, 1, seller)` where `collateralParams[j].oracle` is a reverting oracle. Cost: 1 wei of the collateral token.
3. Bit `j` is set in `position[id][seller].collateralBitmap`.
4. Any taker calls `take(offer, ...)` where the seller is the debt-holding party.
5. `isHealthy(market, id, seller)` is called at line 476.
6. The bitmap loop reaches index `j`, calls `IOracle(collateralParams[j].oracle).price()`, which reverts.
7. `isHealthy` reverts → `take` reverts with no useful error.
8. All takers are blocked from filling any of the seller's offers.

**Why existing checks fail:**
- The `liquidationLocked` short-circuit at line 476 only fires in the reentrant path (`wasLocked == true`). In the normal external call path it is always false after line 475 clears the lock.
- There is no `try/catch` around `IOracle.price()` in `isHealthy`.
- `supplyCollateral` has no oracle liveness check before activating a new collateral index.

**Formal confirmation:** The Certora spec `Reverts.spec` rule `oracleRevertPreventsTakeWhenSellerHasDebt` (lines 224–241) formally proves this revert path is reachable, and its inline comment explicitly notes the `liquidationLocked` short-circuit does not apply in the normal external call path. The `certora/README.md` also documents this as a known property: "A reverting or zero-returning collateral oracle blocks `liquidate`, `withdrawCollateral`, `isHealthy` and `take` whenever the borrower has debt."

## Impact Explanation
Every `take()` call against any offer where the poisoned seller is the debt-holding party reverts at `Midnight.sol:476`. Takers cannot fill those offers at all. The seller's entire offer book is effectively frozen for takers. The seller retains their debt position and collateral; only taker-side liquidity is blocked. This constitutes a permanent, seller-controlled freeze of taker access to a specific offer book, achievable at a cost of 1 wei plus gas.

## Likelihood Explanation
Required preconditions: (a) the market lists at least two collateral params; (b) the seller has non-zero debt; (c) the seller can obtain 1 wei of the token for a collateral index whose oracle reverts or will revert. Condition (c) is feasible whenever a market lists a collateral whose oracle is upgradeable, pauseable, or otherwise fallible. Since market creation is permissionless, an attacker can also deploy a market with a reverting oracle as one of the collateral params and induce others to use it. The seller has direct economic incentive: if market rates move against them after signing offers, freezing takers prevents further debt accrual at unfavorable terms. The attack is repeatable and cheap.

## Recommendation
Two complementary mitigations:

1. **Oracle liveness check in `supplyCollateral`**: Before setting a new bit in `collateralBitmap`, call `IOracle(collateralParam.oracle).price()` inside a `try/catch`. Revert if the oracle reverts, preventing activation of a broken oracle index.

2. **`try/catch` in `isHealthy`**: Wrap the `IOracle(collateralParam.oracle).price()` call in a `try/catch`. On revert, either treat the collateral as contributing zero to `maxDebt` (conservative, favors liquidation) or revert with a descriptive error. The former is safer for liveness; the latter preserves the current strict behavior but at least surfaces a clear error.

Option 1 alone is insufficient if an oracle becomes reverting *after* activation. Option 2 alone changes health semantics. Both together provide defense in depth.

## Proof of Concept
Minimal Foundry test outline:

```solidity
// 1. Deploy market with two collateral params: collateralParams[0] = normal oracle,
//    collateralParams[1] = RevertingOracle (price() always reverts).
// 2. Taker calls take() on seller's offer → seller acquires debt.
// 3. Seller calls supplyCollateral(market, 1, 1, seller) → bit 1 set in bitmap.
// 4. Any address calls take(offer, ...) targeting the same seller.
// 5. Assert: take() reverts (isHealthy reverts due to RevertingOracle.price()).
// 6. Confirm: seller's debt is unchanged, taker received nothing.
```

The Certora rule `oracleRevertPreventsTakeWhenSellerHasDebt` in `certora/specs/Reverts.spec` lines 224–241 is a formal proof of step 5 under the assumption `!liquidationLocked(id, seller)`, which holds in all normal external call paths.