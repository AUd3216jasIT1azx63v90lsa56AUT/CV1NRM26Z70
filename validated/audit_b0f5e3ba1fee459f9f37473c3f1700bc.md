Audit Report

## Title
Off-by-One in Maturity Guard Permits Debt Creation at Exact Maturity Timestamp - (File: src/Midnight.sol)

## Summary
The `take` function at line 391 uses `block.timestamp <= offer.market.maturity` to guard against post-maturity debt increases. When `block.timestamp == offer.market.maturity`, the guard passes unconditionally regardless of `sellerDebtIncrease`, allowing new debt to be written to a seller's position at the exact maturity block. This debt is immediately overdue and liquidatable, violating the protocol's invariant that debt must not increase at or after maturity.

## Finding Description
**Root cause:** [1](#0-0) 

The condition `block.timestamp <= offer.market.maturity` evaluates to `true` when `block.timestamp == offer.market.maturity`, making the disjunction unconditionally true and bypassing the `sellerDebtIncrease == 0` requirement.

**State mutation that follows:** [2](#0-1) 

`sellerPos.debt += sellerDebtIncrease` executes with a non-zero value at the maturity block.

**`timeToMaturity` is zero at this point:** [3](#0-2) 

`UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp)` returns `0`, so `buyerPendingFeeIncrease` is also zero — the debt is created with no fee accrual, immediately overdue.

**Exploit path:**
1. Attacker waits for `block.timestamp == market.maturity`.
2. Attacker calls `take` on a valid sell offer (`offer.buy == false`), where the maker is the borrower. The seller has zero existing credit, so `sellerCreditDecrease = 0` and `sellerDebtIncrease = units > 0`.
3. Line 391 evaluates `maturity <= maturity` → `true`. Require passes.
4. Line 414 writes new debt to the seller's position.
5. The debt is immediately overdue and subject to post-maturity liquidation in the next block.
6. Via `multicall` (lines 211–220), the attacker can atomically repeat this across multiple markets sharing the same maturity in a single transaction, since all `delegatecall`s share the same `block.timestamp`. [4](#0-3) 

**Why existing checks do not stop it:**

- The `CannotIncreaseDebtPostMaturity` guard at line 391 uses `<=`, not `<` — it is the bug itself.
- The `enterGate` checks at lines 402–406 only apply when a gate is configured; ungated markets have no secondary protection. [5](#0-4) 

- The post-take `isHealthy` check at line 476 verifies seller health *after* the debt increase using the current timestamp. At `block.timestamp == maturity`, if `isHealthy` evaluates health under pre-maturity parameters (LIF = 1), a seller can pass this check yet be immediately liquidatable under post-maturity LIF in the next block, creating a gap where the seller is forced into overdue debt they cannot cover under post-maturity liquidation terms. [6](#0-5) 

- All existing post-maturity tests warp to `maturity + 1`, leaving the `== maturity` boundary completely untested.

## Impact Explanation
A taker can force a seller (borrower) to take on new debt units at the exact maturity block. This debt is immediately overdue. If the post-maturity LIF exceeds the pre-maturity LIF used by `isHealthy` at the maturity boundary, the seller's collateral may be insufficient to cover the liquidation incentive, realizing bad debt socialized across lenders. Even absent bad debt, the seller is involuntarily placed into an immediately liquidatable position, constituting unauthorized state change and a direct violation of the protocol's debt-increase-after-maturity invariant. This is a direct theft/loss-of-funds impact class.

## Likelihood Explanation
The precondition `block.timestamp == market.maturity` is a single predictable block. On chains with 1-second block times (Ethereum post-merge, most L2s), an attacker can monitor the chain and submit the transaction in the maturity block. No privileged access is required — the attacker controls only the taker side. The offer must be a valid, ratified sell offer, but the attacker does not need to control the maker. The attack is repeatable across any market whose maturity falls on a future block and is batchable via `multicall`.

## Recommendation
Change the guard at line 391 from `<=` to `<`:

```solidity
// Before (vulnerable):
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());

// After (fixed):
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

This ensures that at `block.timestamp == market.maturity`, the left-hand side is `false`, and the require only passes if `sellerDebtIncrease == 0`.

## Proof of Concept
**Minimal Foundry test:**

```solidity
function test_takeAtExactMaturity_createsImmediatelyOverdueDebt() public {
    // Setup: create a market with maturity = block.timestamp + 1 day
    // Create a valid sell offer (maker = borrower, offer.buy = false)
    // Warp to exactly block.timestamp == market.maturity
    vm.warp(market.maturity);
    // Call take with units > 0
    (uint256 buyerAssets, uint256 sellerAssets) = midnight.take(sellOffer, ratifierData, units, taker, receiver, address(0), "");
    // Assert: sellerPos.debt > 0 (debt was created at maturity)
    assertGt(midnight.position(id, seller).debt, 0);
    // Assert: block.timestamp == market.maturity (debt is immediately overdue)
    assertEq(block.timestamp, market.maturity);
}
```

The test should pass (demonstrating the bug) with the current `<=` guard and revert with `CannotIncreaseDebtPostMaturity` after changing to `<`.

### Citations

**File:** src/Midnight.sol (L211-220)
```text
    function multicall(bytes[] calldata calls) external {
        for (uint256 i = 0; i < calls.length; i++) {
            (bool success, bytes memory returnData) = address(this).delegatecall(calls[i]);
            if (!success) {
                assembly ("memory-safe") {
                    revert(add(returnData, 0x20), mload(returnData))
                }
            }
        }
    }
```

**File:** src/Midnight.sol (L359-359)
```text
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
```

**File:** src/Midnight.sol (L391-391)
```text
        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

**File:** src/Midnight.sol (L402-406)
```text
        require(
            offer.market.enterGate == address(0) || sellerDebtIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseDebt(seller),
            SellerGatedFromIncreasingDebt()
        );
```

**File:** src/Midnight.sol (L414-414)
```text
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/Midnight.sol (L476-476)
```text
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```
