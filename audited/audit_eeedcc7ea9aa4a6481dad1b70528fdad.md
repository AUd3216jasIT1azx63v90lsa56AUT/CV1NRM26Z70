### Title
Missing `oracle != address(0)` Validation in `touchMarket()` Enables Permanent Liquidation DoS — (File: src/Midnight.sol)

### Summary

`touchMarket()` validates `lltv`, `maxLif`, collateral token sorting, and maturity bounds, but never checks that `CollateralParams.oracle != address(0)`. A market with a zero-address oracle can be created and used. Once a borrower activates such a collateral, every call to `liquidate()` and `isHealthy()` reverts, making the position permanently un-liquidatable and trapping lenders in unresolvable bad debt.

### Finding Description

`touchMarket()` performs the following validation loop over each `CollateralParams` entry: [1](#0-0) 

It checks `token > previousCollateralToken` (implicitly non-zero token), `isLltvAllowed(lltv)`, and `maxLif` against two allowed values. There is **no check** that `market.collateralParams[i].oracle != address(0)`.

The Certora formal spec proves `token != address(0)` as an invariant but has no corresponding invariant for `oracle != address(0)`: [2](#0-1) 

Once a market is created with `oracle = address(0)` for one collateral (call it collateral B), the exploit path is:

1. Borrower supplies collateral A (valid oracle) and calls `take()` as seller — succeeds because only A is in the bitmap and `isHealthy` only iterates activated collaterals.
2. Borrower calls `supplyCollateral()` for collateral B — succeeds because `supplyCollateral` makes no oracle call, activating B's bit.
3. Now both A and B are in `_position.collateralBitmap`.

`liquidate()` iterates **all** activated collaterals unconditionally: [3](#0-2) 

`IOracle(address(0)).price()` is called for collateral B. Since `address(0)` has no code, the EVM call returns empty data; Solidity's ABI decoder then reverts trying to decode an empty return as `uint256`. Every `liquidate()` call for this borrower reverts.

`isHealthy()` has the same loop: [4](#0-3) 

So `withdrawCollateral()` for collateral A also reverts (it calls `isHealthy` after updating state). The borrower can attempt to withdraw **all** of collateral B first (which clears B's bit before `isHealthy` runs), but only if they are healthy based on A alone. If the position is already unhealthy (debt > maxDebt from A), that withdrawal also fails the health check, and the borrower is fully stuck.

The `SafeTransferLib` confirms `address(0)` is not a valid token address (it checks `code.length > 0`), but no equivalent guard exists for the oracle field: [5](#0-4) 

### Impact Explanation

**High.** When a borrower is unhealthy and has collateral B (zero oracle) activated:

- `liquidate()` always reverts — bad debt cannot be socialized via the `lossFactor` mechanism.
- `withdrawCollateral()` for collateral A always reverts — borrower's good collateral is locked.
- Lenders holding credit units in this market cannot recover funds through liquidation; the `lossFactor` is never updated, so bad debt is never distributed.

The borrower can self-rescue only by repaying the full debt via `repay()` (which makes no oracle call). If the borrower is insolvent and unwilling to repay, lenders face permanent, unresolvable bad debt.

### Likelihood Explanation

**Low.** Requires user error at market creation time (setting `oracle = address(0)`) and a borrower who subsequently activates that collateral. This is directly analogous to the external report's "Low likelihood, as it requires user error when configuring their recovery." The market is permissionless, so a malicious actor could also create such a market to trap users who do not inspect oracle addresses before borrowing.

### Recommendation

Add a `require(market.collateralParams[i].oracle != address(0), ...)` check inside the `touchMarket()` validation loop, mirroring the existing non-zero token check:

```solidity
// inside the for loop in touchMarket()
require(market.collateralParams[i].oracle != address(0), ZeroOracle());
```

This is consistent with the protocol's existing defense-in-depth approach of validating all critical `CollateralParams` fields at market creation time, and with the Certora invariant already proven for `token != address(0)`. [1](#0-0) 

### Proof of Concept

```
State:
  - Market M: collateralParams = [
      {token: A, oracle: validOracle, lltv: 0.77e18, maxLif: ...},  // index 0
      {token: B, oracle: address(0),  lltv: 0.77e18, maxLif: ...}   // index 1
    ]

Step 1: touchMarket(M) — succeeds (no oracle validation)
Step 2: supplyCollateral(M, 0, 1000e18, borrower) — activates bit 0 for A
Step 3: take(offer, ...) where offer.maker=borrower, offer.buy=false
        → seller=borrower gets debt, isHealthy checks only bit 0 (A) → passes
Step 4: supplyCollateral(M, 1, 1e18, borrower) — activates bit 1 for B (no oracle call)

Now collateralBitmap = 0b11 (both A and B activated)

Step 5: Price of A drops; borrower is unhealthy (debt > maxDebt from A alone)

Step 6: liquidate(M, 0, ..., borrower, ...) 
        → loop hits i=1 (B), calls IOracle(address(0)).price() → REVERT

Step 7: withdrawCollateral(M, 1, 1e18, borrower, ...) [try to remove B]
        → clears bit 1, calls isHealthy → debt > maxDebt from A → REVERT (UnhealthyBorrower)

Step 8: withdrawCollateral(M, 0, any, borrower, ...) [try to remove A]
        → bit 1 still set, isHealthy calls IOracle(address(0)).price() → REVERT

Result: borrower's collateral A permanently locked; liquidation permanently blocked;
        bad debt cannot be socialized.
```

### Citations

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

**File:** src/Midnight.sol (L762-773)
```text
            for (uint256 i = 0; i < market.collateralParams.length; i++) {
                address collateralToken = market.collateralParams[i].token;
                require(collateralToken > previousCollateralToken, CollateralParamsNotSorted());
                uint256 lltv = market.collateralParams[i].lltv;
                require(isLltvAllowed(lltv), LltvNotAllowed());
                require(
                    market.collateralParams[i].maxLif == maxLif(lltv, LIQUIDATION_CURSOR_LOW)
                        || market.collateralParams[i].maxLif == maxLif(lltv, LIQUIDATION_CURSOR_HIGH),
                    InvalidMaxLif()
                );
                previousCollateralToken = collateralToken;
            }
```

**File:** src/Midnight.sol (L949-957)
```text
            uint128 _collateralBitmap = _position.collateralBitmap;
            while (_collateralBitmap != 0) {
                uint256 i = UtilsLib.msb(_collateralBitmap);
                CollateralParams memory collateralParam = market.collateralParams[i];
                uint256 price = IOracle(collateralParam.oracle).price();
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
                _collateralBitmap = _collateralBitmap.clearBit(i);
            }
```

**File:** certora/specs/CreatedMarkets.spec (L65-67)
```text
// Show that a created market do not have address(0) collateralParams.
strong invariant createdMarketsHaveNonZeroCollaterals(Midnight.Market market, uint256 i)
    marketIsCreated(market) => i < market.collateralParams.length => market.collateralParams[i].token != 0;
```

**File:** src/libraries/SafeTransferLib.sol (L12-14)
```text
    function safeTransfer(address token, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());

```
