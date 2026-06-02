Audit Report

## Title
Oracle Price Manipulated via `onSell` Callback Allows Undercollateralized Borrowing - (File: src/Midnight.sol)

## Summary
In `take()`, the seller's debt is committed to storage before the `onSell` callback fires, and the health check runs after the callback completes. Because `IOracle.price()` is declared `external view` and called via `staticcall` inside `isHealthy`, it cannot write state but can freely read state already written by the callback. An attacker who deploys a malicious oracle and a malicious sell callback — both permissionlessly reachable via `touchMarket` and offer creation — can inflate the oracle price during the callback, causing `isHealthy` to pass with a fabricated price and leaving the seller with undercollateralized debt backed by negligible collateral.

## Finding Description
**Exact code path:**

1. `take()` is called (`src/Midnight.sol:337`).
2. `sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease)` at line 414 — seller's debt is committed to storage before any callback.
3. Liquidation lock is set for the seller at line 444.
4. `onSell` callback fires at lines 458–474 via a regular `CALL` (not `staticcall`), so the callback can write to any external contract, including an attacker-controlled `StateContract`.
5. Liquidation lock is released at line 475.
6. `require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable())` at line 476.
7. Inside `isHealthy` at line 953: `uint256 price = IOracle(collateralParam.oracle).price()` — called via `staticcall` because `price()` is `external view`. The oracle reads from the `StateContract` whose storage was written in step 4, returning an inflated price.
8. `maxDebt` is computed with the inflated price and exceeds the seller's actual debt — health check passes.

**Root cause:** The protocol fires an unrestricted external call (`onSell`) between committing the seller's debt increase and evaluating the health check. The oracle is called after the callback, not before, so any state written by the callback is visible to the oracle.

**Why existing checks fail:**
- The liquidation lock (lines 444, 475) only prevents liquidation during callbacks; it does not prevent oracle price manipulation.
- `staticcall` on `price()` prevents the oracle from writing state during the health check, but the callback already wrote the manipulated value before `isHealthy` is called.
- No snapshot of the oracle price is taken before the callback to compare against.
- The Certora formal verification at `certora/specs/Healthiness.spec:14–16` explicitly assumes `price()` is constant during execution (`// Assumption: price does not change during rules. ... function _.price() external => summaryPrice(calledContract) expect(uint256)`) and does not cover this case.

## Impact Explanation
The seller can borrow an arbitrary number of units backed by negligible collateral (e.g., 1 wei). The seller receives `sellerAssets` loan tokens during the take. After `take()` completes, the oracle returns to its real price, leaving the seller's position undercollateralized. Lenders bear the resulting bad debt. This is a direct theft of lender funds and violates the core protocol invariant that every debt position must be backed by sufficient collateral at the real oracle price.

## Likelihood Explanation
All preconditions are fully attacker-controlled and require no privileged access:
- Anyone can call `touchMarket` with a `CollateralParams.oracle` pointing to a malicious oracle that reads from an attacker-controlled `StateContract`.
- The seller controls `offer.callback` (when `offer.buy == false`, seller = `offer.maker`) or `takerCallback` (when `offer.buy == true`, seller = taker).
- The attacker deploys three contracts (`StateContract`, malicious oracle, malicious callback), creates a market, and creates or takes an offer. No governance, admin, or leaked-key access is needed.
- The attack is repeatable across any number of takes and is not blocked by any on-chain guard.

## Recommendation
Snapshot the oracle price(s) before the `onSell` callback fires and re-use the snapshot inside `isHealthy` for the post-callback health check, rather than re-querying the oracle after the callback. Alternatively, call `isHealthy` (or at minimum query all oracle prices) before the `onSell` callback and require that the post-callback health check uses the same prices. A simpler mitigation is to move the `onSell` callback after the health check, so the health check runs on committed state before any external call can influence oracle-readable storage.

## Proof of Concept
1. Deploy `StateContract` with a `uint256 price` slot, initially set to a real low value.
2. Deploy `MaliciousOracle` implementing `IOracle.price()` as `return StateContract.price()`.
3. Deploy `MaliciousCallback` implementing `ISellCallback.onSell(...)` as `StateContract.setPrice(type(uint256).max / 2)`.
4. Call `touchMarket` with `CollateralParams.oracle = address(MaliciousOracle)`.
5. Supply 1 wei of collateral to the market.
6. Create a sell offer with `offer.callback = address(MaliciousCallback)` for a large `units` value.
7. Take the offer (from a second address or self).
8. Observe: `onSell` fires, `StateContract.price` is set to `type(uint256).max / 2`, `isHealthy` returns `true` with the inflated price, and the seller now holds large debt backed by 1 wei collateral.
9. Confirm: after the transaction, `StateContract.price` remains inflated (or can be reset), and the seller's position is undercollateralized at the real price, creating bad debt for lenders.