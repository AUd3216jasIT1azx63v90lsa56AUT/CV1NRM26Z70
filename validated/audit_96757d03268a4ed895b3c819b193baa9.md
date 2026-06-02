Audit Report

## Title
Off-by-One in Maturity Guard Permits Debt Creation at Exact Maturity Timestamp - (File: src/Midnight.sol)

## Summary
The `take` function uses `block.timestamp <= offer.market.maturity` as its post-maturity debt guard. When `block.timestamp == offer.market.maturity`, the left operand is unconditionally `true`, bypassing the `sellerDebtIncrease == 0` requirement and allowing new debt to be written to a seller's position at the exact maturity block. This debt is immediately eligible for post-maturity liquidation starting in the next block, violating the protocol's invariant that debt must not increase at or after maturity.

## Finding Description
**Root cause — `src/Midnight.sol` line 391:**
```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
When `block.timestamp == offer.market.maturity`, the disjunction is unconditionally `true` regardless of `sellerDebtIncrease`. The guard should use strict `<`.

**State mutation path:**
- Line 359: `timeToMaturity = zeroFloorSub(maturity, maturity) = 0`, so `buyerPendingFeeIncrease = 0` (line 386). Debt is created with no pending fee — immediately overdue.
- Line 384: `sellerDebtIncrease = units - sellerCreditDecrease`. For a seller with zero existing credit, `sellerCreditDecrease = 0` and `sellerDebtIncrease = units`.
- Line 414: `sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease)` executes unconditionally with a non-zero value.

**Post-maturity liquidation eligibility:**
Line 622 checks `postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt`. The strict `>` means post-maturity liquidation is not available at the exact maturity block, but becomes available from `maturity + 1`. The LIF at `maturity + 1` is `WAD + (maxLif - WAD) * 1 / TIME_TO_MAX_LIF` — above WAD and growing linearly.

**`isHealthy` (lines 944–959):** Checks `collateral * price * lltv / WAD >= debt` using only `lltv`, not LIF. A seller with sufficient collateral passes this check at maturity, allowing the take to succeed.

**Exploit path:**
1. Attacker identifies a market with a future maturity and a valid, ratified sell offer where the maker (seller) has zero existing credit.
2. Attacker waits for `block.timestamp == market.maturity`.
3. Attacker calls `take` with `units > 0`. Line 391 evaluates `maturity <= maturity → true`; require passes.
4. Line 414 writes `sellerDebtIncrease = units` to the seller's position with zero pending fee.
5. Line 476 checks `isHealthy` — if the seller has collateral satisfying `lltv`, the take succeeds.
6. From `maturity + 1`, the seller's debt is subject to post-maturity liquidation with LIF growing above 1, placing them in an involuntarily liquidatable position they never consented to.
7. Via `multicall` (lines 211–220, using `delegatecall` with shared `block.timestamp`), the attacker can atomically repeat this across multiple markets sharing the same maturity in a single transaction.

**Why existing checks fail:**
- The `CannotIncreaseDebtPostMaturity` guard at line 391 is the bug itself (`<=` instead of `<`).
- `enterGate` checks at lines 402–406 only apply when a gate is configured; ungated markets have no secondary protection.
- All existing post-maturity tests warp to `maturity + 1` (test lines 349–350, 372–373, 391–392), leaving the `== maturity` boundary completely untested.

## Impact Explanation
A taker can force a seller (borrower) to take on new debt units at the exact maturity block without the seller's consent for that specific timing. This debt carries zero pending fee and is immediately eligible for post-maturity liquidation starting in the next block. As the post-maturity LIF grows linearly from 1 toward `maxLif`, the seller's collateral may be seized at an increasingly unfavorable rate. If the seller's collateral is insufficient to cover the growing LIF, bad debt is realized and socialized across lenders. Even absent bad debt, the seller is involuntarily placed into an immediately post-maturity-liquidatable position — unauthorized state change and a direct violation of the protocol's debt-increase-after-maturity invariant. Impact class: unauthorized state change / potential loss of funds.

## Likelihood Explanation
The precondition `block.timestamp == market.maturity` is a single predictable block. On chains with 1-second block times (Ethereum post-merge, most L2s), an attacker can monitor the chain and submit the transaction in the maturity block with no privileged access. The attacker controls only the taker side; the offer must be a valid, ratified sell offer, but the attacker does not need to control the maker. The attack is repeatable across any market whose maturity falls on a future block and is batchable via `multicall`.

## Recommendation
Change `<=` to `<` at line 391:
```solidity
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
This ensures the guard rejects any debt-increasing take at or after maturity, consistent with the protocol's invariant.

## Proof of Concept
```solidity
// Minimal Foundry test demonstrating the boundary bypass:
function testDebtCreatedAtExactMaturity() public {
    uint256 units = 100;
    // Setup: seller has zero credit, sufficient collateral
    collateralize(market, seller, units);
    setupMarket(market, units);

    // Warp to exact maturity (not maturity + 1)
    vm.warp(market.maturity);
    sellerOffer.expiry = market.maturity;
    sellerOffer.maxUnits = units;
    deal(address(loanToken), buyer, units);

    // This should revert with CannotIncreaseDebtPostMaturity but does not
    take(units, buyer, sellerOffer);

    // Seller now has debt at maturity — invariant violated
    assertGt(midnight.debtOf(id, seller), 0);

    // One block later: seller is immediately post-maturity liquidatable
    vm.warp(market.maturity + 1);
    midnight.liquidate(market, 0, 0, 0, seller, true, address(this), address(0), "");
}
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) [10](#0-9)

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

**File:** src/Midnight.sol (L384-386)
```text
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
        uint128 buyerPendingFeeIncrease =
            UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
```

**File:** src/Midnight.sol (L391-391)
```text
        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

**File:** src/Midnight.sol (L414-414)
```text
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/Midnight.sol (L645-647)
```text
            uint256 lif = postMaturityMode
                ? UtilsLib.min(_maxLif, WAD + (_maxLif - WAD) * (block.timestamp - market.maturity) / TIME_TO_MAX_LIF)
                : _maxLif;
```

**File:** src/Midnight.sol (L944-959)
```text
    function isHealthy(Market memory market, bytes32 id, address borrower) public view returns (bool) {
        Position storage _position = position[id][borrower];
        uint256 debt = _position.debt;
        uint256 maxDebt;
        if (debt > 0) {
            uint128 _collateralBitmap = _position.collateralBitmap;
            while (_collateralBitmap != 0) {
                uint256 i = UtilsLib.msb(_collateralBitmap);
                CollateralParams memory collateralParam = market.collateralParams[i];
                uint256 price = IOracle(collateralParam.oracle).price();
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
                _collateralBitmap = _collateralBitmap.clearBit(i);
            }
        }
        return maxDebt >= debt;
```

**File:** test/TakeTest.sol (L349-350)
```text
        uint256 timestamp = market.maturity + 1;
        vm.warp(timestamp);
```

**File:** test/TakeTest.sol (L372-373)
```text
        uint256 timestamp = market.maturity + 1;
        vm.warp(timestamp);
```
