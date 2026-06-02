Audit Report

## Title
Borrower Self-Immunizes Against Liquidation by Activating a Reverting-Oracle Collateral Slot - ([File: src/Midnight.sol])

## Summary
`supplyCollateral` never calls any oracle and has no health check, allowing a borrower to activate a collateral slot backed by a reverting oracle at any time—including after becoming unhealthy. `liquidate` unconditionally iterates every bit in `collateralBitmap` and calls `IOracle.price()` on each with no error handling, so a single reverting oracle causes the entire liquidation transaction to revert. An unhealthy borrower who supplies 1 wei of a reverting-oracle collateral becomes permanently unliquidatable, leaving bad debt frozen on the books and lenders unable to recover funds.

## Finding Description
**Root cause — asymmetric oracle access between `supplyCollateral` and `liquidate`.**

`supplyCollateral` (lines 524–546) performs no oracle call. Its only guard when activating a new slot is the 16-collateral cap:

```solidity
if (oldCollateral == 0 && assets > 0) {
    uint128 newCollateralBitmap = _position.collateralBitmap.setBit(collateralIndex);
    _position.collateralBitmap = newCollateralBitmap;
    require(
        UtilsLib.countBits(newCollateralBitmap) <= MAX_COLLATERALS_PER_BORROWER,
        TooManyActivatedCollaterals()
    );
}
``` [1](#0-0) 

`liquidate` (lines 607–618) iterates every set bit in `collateralBitmap` and calls `price()` on each oracle with no try/catch or skip-on-revert logic:

```solidity
while (_collateralBitmap != 0) {
    uint256 i = UtilsLib.msb(_collateralBitmap);
    CollateralParams memory _collateralParam = market.collateralParams[i];
    uint256 price = IOracle(_collateralParam.oracle).price(); // reverts if oracle reverts
    ...
    _collateralBitmap = _collateralBitmap.clearBit(i);
}
``` [2](#0-1) 

The protocol's own NatSpec explicitly acknowledges both halves of the problem: [3](#0-2) [4](#0-3) 

**Exploit flow:**
1. A multi-collateral market exists with `collateralParams[0]` (healthy oracle) and `collateralParams[1]` (oracle that reverts—e.g., a Chainlink feed with a triggered circuit-breaker, or a purpose-deployed contract).
2. Borrower supplies collateral at index 0 and borrows via `take`.
3. Price of index-0 collateral drops; borrower becomes unhealthy (`debt > maxDebt`).
4. Borrower calls `supplyCollateral(market, 1, 1, borrower)`. No oracle is called; 1 wei of the index-1 token is transferred in and bit 1 is set in `collateralBitmap`. This succeeds unconditionally.
5. Any subsequent `liquidate(market, 0, ...)` enters the bitmap loop, hits `IOracle(collateralParams[1].oracle).price()`, which reverts, and the entire transaction reverts.
6. `isHealthy` reverts for the same reason, so `withdrawCollateral` (when borrower has debt) also reverts—but the critical failure is liquidation.

**Why existing checks do not stop it:**
- The `Unauthorized` check in `supplyCollateral` is satisfied because the borrower calls on their own behalf.
- `TooManyActivatedCollaterals` only fires at 17 active slots; a borrower with fewer than 16 active collaterals can always add one more.
- There is no health check in `supplyCollateral`; the function is explicitly designed to never call an oracle.
- There is no try/catch or skip-on-revert logic anywhere in the `liquidate` bitmap loop.

The test suite even ships a `RevertingOracle` contract confirming the team is aware of this scenario: [5](#0-4) 

## Impact Explanation
An unhealthy borrower with outstanding debt becomes permanently unliquidatable. Their debt remains on the books, `marketState.totalUnits` is never reduced via bad-debt socialization, and lenders cannot recover their funds. Every lender in the market suffers a loss proportional to the bad debt that cannot be realized. The `withdrawable` pool is never replenished, so lenders who try to withdraw after maturity also find insufficient assets. This constitutes a permanent freeze of lender funds and unrecoverable bad debt—a critical, in-scope impact.

## Likelihood Explanation
**Preconditions:**
- A multi-collateral market where at least one collateral's oracle can revert. This is realistic: Chainlink aggregators revert when the sequencer is down (L2s), when the answer is stale beyond a configured threshold, or when a circuit-breaker fires. Custom oracles may revert on price deviation. The market need not be created by the attacker.
- The borrower must hold (or be able to acquire) at least 1 wei of the index-1 collateral token. For any ERC-20 with non-zero supply this is trivially achievable.
- The borrower must act before being liquidated, i.e., between becoming unhealthy and the first successful liquidation call. Given that oracle outages are often sustained (minutes to hours), this window is realistic.

**Repeatability:** The attack is permanent once executed. The borrower cannot be forced to withdraw the 1-wei collateral (only they or an authorized address can call `withdrawCollateral`, and that function also reverts when the borrower has debt and any oracle reverts). The state is irreversible without a protocol upgrade.

## Recommendation
Add try/catch error handling in the `liquidate` bitmap loop so that a reverting oracle for a non-liquidated collateral slot does not abort the entire transaction. One approach: if `IOracle.price()` reverts for a collateral that is not the one being liquidated, skip it for `maxDebt`/`badDebt` computation (treating its contribution as zero, which is conservative for the liquidator). Alternatively, add a health check in `supplyCollateral` that prevents activating a new collateral slot when the borrower already has debt and the resulting position would be unhealthy or the new oracle reverts. A third option is to require that `supplyCollateral` calls the oracle for the new slot before activating it, ensuring only live oracles can be activated.

## Proof of Concept
Minimal Foundry test outline using the existing `RevertingOracle` helper: [6](#0-5) 

1. Deploy a two-collateral market: `collateralParams[0]` uses a normal oracle, `collateralParams[1]` uses `RevertingOracle` (initially not stopped).
2. Borrower supplies collateral at index 0 and calls `take` to borrow.
3. Call `oracle0.setPrice(lowPrice)` to make the borrower unhealthy; confirm `isHealthy` returns false.
4. Call `revertingOracle.stopOracle()` to make index-1 oracle revert.
5. Borrower calls `supplyCollateral(market, 1, 1, borrower)` — expect success.
6. Liquidator calls `liquidate(market, 0, 0, repaidUnits, borrower, false, ...)` — expect revert.
7. Assert the borrower's debt is unchanged and the position remains unliquidatable.

### Citations

**File:** src/Midnight.sol (L34-36)
```text
/// @dev Liquidation reverts if any of the activated collaterals' oracle reverts (see LIVENESS).
/// @dev Note that a borrower can activate a collateral once its oracle is reverting because the oracle is not called in
/// supplyCollateral.
```

**File:** src/Midnight.sol (L143-145)
```text
/// @dev If an activated collateral oracle reverts on price, liquidate reverts.
/// @dev If an activated collateral oracle reverts on price, isHealthy, withdrawCollateral and take revert when the user
/// (seller for take) has non-zero debt.
```

**File:** src/Midnight.sol (L535-541)
```text
        if (oldCollateral == 0 && assets > 0) {
            uint128 newCollateralBitmap = _position.collateralBitmap.setBit(collateralIndex);
            _position.collateralBitmap = newCollateralBitmap;
            require(
                UtilsLib.countBits(newCollateralBitmap) <= MAX_COLLATERALS_PER_BORROWER, TooManyActivatedCollaterals()
            );
        }
```

**File:** src/Midnight.sol (L607-618)
```text
        while (_collateralBitmap != 0) {
            uint256 i = UtilsLib.msb(_collateralBitmap);
            CollateralParams memory _collateralParam = market.collateralParams[i];
            uint256 price = IOracle(_collateralParam.oracle).price();
            if (i == collateralIndex) liquidatedCollatPrice = price;
            uint256 _collateral = _position.collateral[i];
            maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(_collateralParam.lltv, WAD);
            badDebt = badDebt.zeroFloorSub(
                _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
            );
            _collateralBitmap = _collateralBitmap.clearBit(i);
        }
```

**File:** test/helpers/RevertingOracle.sol (L1-17)
```text
// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (c) 2025 Morpho Association
pragma solidity ^0.8.0;

contract RevertingOracle {
    uint256 internal _price = 1e36;
    bool internal stopped;

    function price() external view returns (uint256) {
        require(!stopped, "Oracle should not be called");
        return _price;
    }

    function stopOracle() external {
        stopped = true;
    }
}
```
