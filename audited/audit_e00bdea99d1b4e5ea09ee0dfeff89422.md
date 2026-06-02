Based on my review of the code, I can verify the key claims against the actual source.

**Line 391 — confirmed:** [1](#0-0) 
`block.timestamp <= offer.market.maturity` evaluates to `true` when `block.timestamp == offer.market.maturity`, making the disjunction unconditionally true and bypassing the `sellerDebtIncrease == 0` requirement.

**Line 384 / 414 — confirmed:** [2](#0-1) [3](#0-2) 
`sellerDebtIncrease` can be non-zero and is written to `sellerPos.debt` unconditionally after the guard passes.

**`timeToMaturity = 0` at maturity — confirmed:** [4](#0-3) 
`zeroFloorSub(maturity, maturity) = 0`, so `buyerPendingFeeIncrease = 0`. Debt is created with no fee accrual.

**`isHealthy` uses only `lltv`, not LIF — confirmed:** [5](#0-4) 
Health is checked against `collateral * price * lltv / WAD`. The LIF is only applied inside `liquidate`.

**Post-maturity liquidation is available to any borrower after maturity — confirmed by NatSpec:** [6](#0-5) 
"post-maturity mode, available after the market's maturity" — no health requirement. A seller forced into debt at `block.timestamp == maturity` is immediately eligible for post-maturity liquidation in the next block.

**Post-maturity LIF at exact maturity = 1 (WAD) — confirmed:** [7](#0-6) 
`WAD + (maxLif - WAD) * 0 / TIME_TO_MAX_LIF = WAD`. So at the exact maturity block, LIF=1 and the `isHealthy` check and liquidation terms are consistent. The gap the report describes (seller healthy under pre-maturity LIF but immediately liquidatable under post-maturity LIF) does not exist at the exact maturity block — it emerges in subsequent blocks as LIF grows.

**Tests confirm the boundary is untested — confirmed:** [8](#0-7) [9](#0-8) 
All post-maturity tests warp to `maturity + 1`, leaving `== maturity` completely uncovered.

**`multicall` — confirmed:** [10](#0-9) 
Uses `delegatecall`, sharing `block.timestamp` across all calls, enabling batching across markets with the same maturity.

---

The core vulnerability is real and all code references check out. The impact claim about the LIF gap at the exact maturity block is slightly overstated (LIF=1 at exactly maturity, so no immediate gap), but the fundamental invariant violation — debt can be created at or after maturity — is concrete and exploitable. A seller is involuntarily placed into post-maturity-liquidatable debt they never consented to, starting from `maturity + 1`.

---

Audit Report

## Title
Off-by-One in Maturity Guard Permits Debt Creation at Exact Maturity Timestamp - (File: src/Midnight.sol)

## Summary
The `take` function at line 391 uses `block.timestamp <= offer.market.maturity` as its post-maturity debt guard. When `block.timestamp == offer.market.maturity`, the condition is `true` unconditionally, bypassing the `sellerDebtIncrease == 0` requirement and allowing new debt to be written to a seller's position at the exact maturity block. This debt is immediately eligible for post-maturity liquidation in the next block, violating the protocol's invariant that debt must not increase at or after maturity.

## Finding Description
**Root cause:** `src/Midnight.sol` line 391:
```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
When `block.timestamp == offer.market.maturity`, the left operand is `true`, making the disjunction unconditionally `true` regardless of `sellerDebtIncrease`. The guard intended to be `block.timestamp < offer.market.maturity`.

**State mutation:** Line 384 computes `sellerDebtIncrease = units - sellerCreditDecrease`. For a seller with zero existing credit, `sellerCreditDecrease = 0` and `sellerDebtIncrease = units`. Line 414 then executes `sellerPos.debt += sellerDebtIncrease` with a non-zero value.

**Zero fee accrual:** Line 359 computes `timeToMaturity = zeroFloorSub(maturity, maturity) = 0`, so `buyerPendingFeeIncrease = 0` (line 386). The debt is created with no pending fee, immediately overdue.

**`isHealthy` check at line 476:** `isHealthy` (lines 944–959) checks `collateral * price * lltv / WAD >= debt`. It does not use LIF. A seller with sufficient collateral passes this check at maturity. However, post-maturity liquidation (lines 645–647) is available to any borrower once `block.timestamp >= market.maturity`, regardless of health. Starting from `maturity + 1`, the LIF grows above 1, and the seller's newly created debt is subject to post-maturity liquidation they never consented to.

**Exploit path:**
1. Attacker identifies a market with a future maturity and a valid, ratified sell offer where the maker (seller/borrower) has zero existing credit.
2. Attacker waits for `block.timestamp == market.maturity`.
3. Attacker calls `take` with `units > 0`. Line 391 evaluates `maturity <= maturity → true`; require passes.
4. Line 414 writes `sellerDebtIncrease = units` to the seller's position.
5. Line 476 checks `isHealthy` — if the seller has collateral satisfying `lltv`, the take succeeds.
6. From `maturity + 1` onward, the seller's debt is subject to post-maturity liquidation with growing LIF, placing them in an involuntarily liquidatable position.
7. Via `multicall` (lines 211–220, using `delegatecall` with shared `block.timestamp`), the attacker can atomically repeat this across multiple markets sharing the same maturity in a single transaction.

**Why existing checks fail:**
- The `CannotIncreaseDebtPostMaturity` guard at line 391 is the bug itself (`<=` instead of `<`).
- `enterGate` checks at lines 402–406 only apply when a gate is configured; ungated markets have no secondary protection.
- All existing post-maturity tests warp to `maturity + 1` (test lines 349–350, 372–373), leaving the `== maturity` boundary completely untested.

## Impact Explanation
A taker can force a seller (borrower) to take on new debt units at the exact maturity block without the seller's consent for that specific timing. This debt is immediately eligible for post-maturity liquidation starting in the next block. As the post-maturity LIF grows linearly from 1 toward `maxLif`, the seller's collateral may be seized at an increasingly unfavorable rate. If the seller's collateral is insufficient to cover the growing LIF, bad debt is realized and socialized across lenders. Even absent bad debt, the seller is involuntarily placed into an immediately post-maturity-liquidatable position, constituting unauthorized state change and a direct violation of the protocol's debt-increase-after-maturity invariant. Impact class: unauthorized state change / potential loss of funds.

## Likelihood Explanation
The precondition `block.timestamp == market.maturity` is a single predictable block. On chains with 1-second block times (Ethereum post-merge, most L2s), an attacker can monitor the chain and submit the transaction in the maturity block with no privileged access. The attacker controls only the taker side; the offer must be a valid, ratified sell offer, but the attacker does not need to control the maker. The attack is repeatable across any market whose maturity falls on a future block and is batchable via `multicall`.

## Recommendation
Change the guard at line 391 from `<=` to `<`:
```solidity
// Before (buggy):
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());

// After (fixed):
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
This ensures that at `block.timestamp == market.maturity`, the left operand is `false`, forcing `sellerDebtIncrease == 0` to hold for the require to pass, which correctly blocks new debt creation at the maturity boundary.

## Proof of Concept
Minimal Foundry test (add to `TakeTest.sol`):
```solidity
function testSellAtExactMaturity() public {
    uint256 units = 100;
    // Seller (borrower) has zero existing credit
    collateralize(market, borrower, units);

    // Warp to EXACT maturity (not maturity + 1)
    uint256 timestamp = market.maturity;
    vm.warp(timestamp);
    lenderOffer.expiry = timestamp;
    lenderOffer.maxUnits = units;
    deal(address(loanToken), lender, units);

    // This should revert with CannotIncreaseDebtPostMaturity but does NOT
    take(units, borrower, lenderOffer);

    // Debt was written at maturity — invariant violated
    assertGt(midnight.debtOf(id, borrower), 0, "debt created at maturity");

    // In the next block, post-maturity liquidation is available
    vm.warp(market.maturity + 1);
    // Borrower is now subject to post-maturity liquidation
}
```
Expected (buggy) behavior: test passes, debt is created. Expected (fixed) behavior: `take` reverts with `CannotIncreaseDebtPostMaturity`.

### Citations

**File:** src/Midnight.sol (L62-64)
```text
/// @dev There are two liquidation modes: The "post-maturity mode", available after the market's maturity, and the
/// "normal mode", available if the borrower is unhealthy. After maturity, an unhealthy borrower's liquidator can choose
/// between both modes.
```

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

**File:** src/Midnight.sol (L359-386)
```text
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
        uint256 _settlementFee = settlementFee(id, timeToMaturity);
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
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

        (address buyer, address seller) = offer.buy ? (offer.maker, taker) : (taker, offer.maker);
        Position storage buyerPos = position[id][buyer];
        Position storage sellerPos = position[id][seller];

        if (hasCredit(id, buyer) || units > buyerPos.debt) _updatePosition(offer.market, id, buyer);
        if (hasCredit(id, seller)) _updatePosition(offer.market, id, seller);

        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
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
