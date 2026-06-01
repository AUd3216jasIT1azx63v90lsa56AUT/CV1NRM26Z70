Audit Report

## Title
Oracle Price Manipulated via `onSell` Callback Allows Undercollateralized Borrowing - (File: src/Midnight.sol)

## Summary
In `take()`, the seller's debt is committed to storage before the `onSell` callback fires, and `isHealthy` is called after the callback returns. Because `IOracle.price()` is declared `external view` and called via `staticcall`, the oracle cannot write state during the health check — but it can freely read state that the callback already wrote. An attacker who deploys a malicious oracle reading from an attacker-controlled `StateContract`, and a malicious `onSell` callback writing to that same contract, can inflate the oracle price during the callback window, causing `isHealthy` to pass with a fabricated price and leaving the seller with undercollateralized debt.

## Finding Description
**Verified code path in `src/Midnight.sol`:**

1. `take()` is called (line 337).
2. `sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease)` (line 414) — seller's debt is committed to storage.
3. Liquidation lock is set for the seller (line 444).
4. `onSell` callback fires (lines 458–474) — this is a regular `CALL`, not a `staticcall`. The callback can write to any external contract, including an attacker-controlled `StateContract`.
5. Liquidation lock is released (line 475).
6. `require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable())` (line 476).
7. Inside `isHealthy`, `IOracle(collateralParam.oracle).price()` is called (line 953). Because `IOracle.price()` is declared `external view` (`src/interfaces/IOracle.sol:6`), Solidity issues a `staticcall`. The `staticcall` prevents the oracle from writing state during its own execution, but it does not prevent the oracle from reading state that was already written by the callback in step 4. A malicious oracle that reads `StateContract.value` returns the inflated value written by the callback.
8. `maxDebt` is computed with the inflated price and exceeds the seller's actual debt — health check passes.

**Why existing checks fail:**
- The liquidation lock (line 444/475) only prevents liquidation during callbacks; it does not prevent oracle price manipulation.
- `staticcall` on `price()` prevents the oracle from writing state during the health check, but the callback already wrote the manipulated value before `isHealthy` is called.
- No snapshot of the oracle price is taken before the callback to compare against.
- The Certora formal verification in `certora/specs/Healthiness.spec` line 14–16 explicitly acknowledges this gap: `// Assumption: price does not change during rules.` and models `price()` as a persistent ghost that returns a fixed value per oracle address. This assumption is not enforced on-chain.

**Attacker-controlled inputs:**
- Market creation: `touchMarket` is permissionless. The attacker deploys a `StateContract`, a `MaliciousOracle` that reads `StateContract.value`, and a `MaliciousCallback` that writes to `StateContract.value`. They call `touchMarket` with `CollateralParams.oracle = address(MaliciousOracle)`.
- Sell callback: when `offer.buy == true`, the seller is the taker and controls `takerCallback`. When `offer.buy == false`, the seller is `offer.maker` and controls `offer.callback`.

## Impact Explanation
The seller can borrow an arbitrary number of units backed by negligible collateral (e.g., 1 wei). After `take()` completes, the seller's position is undercollateralized at the true oracle price. The attacker can then call `withdrawCollateral`, which also calls `isHealthy` with the same manipulable oracle (line 953), and withdraw all collateral while the `StateContract.value` remains inflated. Once collateral is withdrawn, the attacker resets `StateContract.value` to zero, leaving the protocol with bad debt that is socialized to lenders via the loss factor mechanism (lines 631–634). This directly violates the invariant that every debt unit must be backed by sufficient collateral at the oracle price.

## Likelihood Explanation
All preconditions are fully attacker-controlled: the attacker deploys three contracts (`StateContract`, `MaliciousOracle`, `MaliciousCallback`), creates a market, and supplies minimal collateral. No privileged access is required. The `live_context.json` explicitly lists "market creator" and "callback receiver" as valid attacker profiles. The attack is repeatable across any number of takes and is not blocked by any on-chain guard. The only cost is gas and a small collateral deposit. A victim lender who provides a buy offer in the malicious market (or is attracted by favorable rates) is the counterparty.

## Recommendation
Snapshot the oracle price(s) for the seller's activated collaterals before the callback executes, and use those snapshots in the post-callback `isHealthy` check rather than re-querying the oracle after the callback. Alternatively, move the `isHealthy` check to before the callback fires (after debt is committed), accepting that the callback cannot be used to supply collateral in the same transaction. A third option is to call `isHealthy` via `staticcall` on the entire `take()` tail (from debt commitment onward), but this is architecturally more invasive.

## Proof of Concept
**Minimal Foundry test plan:**

1. Deploy `StateContract` with a `uint256 public value` storage variable.
2. Deploy `MaliciousOracle` implementing `IOracle`: `function price() external view returns (uint256) { return StateContract(sc).value(); }`.
3. Deploy `MaliciousCallback` implementing `ISellCallback`: `onSell(...)` writes `StateContract(sc).setValue(INFLATED_PRICE)` and returns `CALLBACK_SUCCESS`.
4. Create a market via `touchMarket` with `CollateralParams.oracle = address(MaliciousOracle)` and a valid LLTV/maxLif pair.
5. Initialize `StateContract.value` to a legitimate price (e.g., `1e36`). Supply 1 wei of collateral for the attacker-borrower.
6. Have a lender create a buy offer for `N` units.
7. Attacker calls `take(offer, ..., N, attacker, attacker, address(MaliciousCallback), ...)`.
8. Assert: `midnight.debtOf(id, attacker) == N` and `midnight.isHealthy(market, id, attacker) == true` (with inflated price).
9. Reset `StateContract.value` to `0`. Assert: `midnight.isHealthy(market, id, attacker) == false` (position is undercollateralized at true price).
10. Confirm attacker can call `withdrawCollateral` while `StateContract.value` is still inflated, removing all collateral and leaving bad debt.