Audit Report

## Title
Missing oracle address validation in `touchMarket` enables permanent liquidation DoS - (File: src/Midnight.sol)

## Summary
The `touchMarket` function validates collateral token address, LLTV tier, and `maxLif` for each collateral slot but imposes no constraint on `collateralParams[i].oracle`. An unprivileged market creator can deploy a market with `oracle = address(0)` for any collateral slot. Once a borrower activates that slot, every subsequent call to `liquidate` unconditionally invokes `IOracle(address(0)).price()` inside the bitmap loop, which reverts because `address(0)` has no code and returns empty data that cannot be ABI-decoded as `uint256`, permanently freezing lender funds.

## Finding Description

**Root cause — `touchMarket` validation loop:**

The loop at lines 762–772 enforces ordering, LLTV allowance, and `maxLif` validity, but contains no check on the oracle field: [1](#0-0) 

There is no `require(market.collateralParams[i].oracle != address(0))`.

**Trigger — `liquidate` bitmap loop:**

The call at line 610 is unconditional: [2](#0-1) 

If `_collateralParam.oracle == address(0)`, the EVM CALL to `address(0)` returns 0 bytes; Solidity 0.8.x's ABI decoder for `uint256` reverts on empty returndata, causing `liquidate` to always revert.

**Market ID locks in the zero oracle:**

The market ID is derived from a full hash of the market struct. A liquidator must pass the exact market struct used at creation time and cannot substitute a valid oracle address without producing a different (uncreated) market ID.

**Certora invariant gap:**

The `createdMarketsHaveNonZeroCollaterals` invariant only proves `token != 0`, not `oracle != 0`: [3](#0-2) 

The oracle revert propagation rules in `Reverts.spec` confirm the revert behavior — they model it, not prevent it.

**Exploit flow:**
1. Attacker calls `touchMarket` with `collateralParams[i].oracle = address(0)` — passes all validation.
2. Borrower supplies collateral at index `i` and takes a loan; bit `i` is set in `collateralBitmap`.
3. Position becomes unhealthy (price drop or post-maturity).
4. Any liquidator calls `liquidate` with the correct market struct.
5. The loop reaches bit `i`, executes `IOracle(address(0)).price()`.
6. `address(0)` has no code; CALL returns 0 bytes; ABI decoder reverts.
7. `liquidate` always reverts; the unhealthy position can never be liquidated.

## Impact Explanation
Lenders' funds are frozen in a permanently unliquidatable position. Bad debt can never be realized through `liquidate`. Both "permanent or long-term fund freeze" and "liquidation bypass" are explicitly listed as `best_bug_classes` in `live_context.json`. [4](#0-3) 

This is not a "pure external oracle failure" (which is excluded) — it is a protocol-level validation gap that allows a structurally invalid market to be created.

## Likelihood Explanation
Market creation is fully permissionless — "market creator" is listed as an unprivileged attacker role in `live_context.json`. [5](#0-4) 

The attacker only needs to call `touchMarket` with a crafted `CollateralParams` array containing `oracle = address(0)`. The condition is trivially reachable in a single transaction with no capital requirement. The DoS is permanent and repeatable across any number of markets.

## Recommendation
Add a non-zero oracle check inside the `touchMarket` validation loop:

```solidity
// src/Midnight.sol, inside the for loop at lines 762–772
require(market.collateralParams[i].oracle != address(0), InvalidOracle());
```

Additionally, add a corresponding Certora invariant `createdMarketsHaveNonZeroOracles` analogous to `createdMarketsHaveNonZeroCollaterals`. [6](#0-5) 

## Proof of Concept

```solidity
// Minimal Foundry test
function test_zerOraclePermanentDoS() public {
    CollateralParams[] memory params = new CollateralParams[](1);
    params[0] = CollateralParams({
        token: address(collateralToken),
        oracle: address(0),          // zero oracle — passes touchMarket validation
        lltv: ALLOWED_LLTV,
        maxLif: maxLif(ALLOWED_LLTV, LIQUIDATION_CURSOR_LOW)
    });
    Market memory market = Market({ loanToken: address(loanToken), collateralParams: params, ... });

    // Step 1: create market — succeeds
    bytes32 id = midnight.touchMarket(market);

    // Step 2: borrower supplies collateral and borrows
    vm.prank(borrower);
    midnight.supplyCollateral(market, 0, 1e18, borrower);
    // ... take offer to create debt ...

    // Step 3: make position unhealthy (warp past maturity or drop price)
    vm.warp(market.maturity + 1);

    // Step 4: liquidate — always reverts
    vm.expectRevert();
    midnight.liquidate(market, 0, 0, 0, borrower, true, liquidator, address(0), "");
}
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

**File:** certora/specs/CreatedMarkets.spec (L65-67)
```text
// Show that a created market do not have address(0) collateralParams.
strong invariant createdMarketsHaveNonZeroCollaterals(Midnight.Market market, uint256 i)
    marketIsCreated(market) => i < market.collateralParams.length => market.collateralParams[i].token != 0;
```

**File:** live_context.json (L30-42)
```json
    "attacker_model": [
      "unprivileged borrower",
      "unprivileged lender",
      "maker",
      "taker",
      "liquidator",
      "market creator",
      "callback receiver",
      "authorized operator",
      "signature user",
      "router/composer using multicall"
    ],
    "do_not_rely_on": [
```

**File:** live_context.json (L53-66)
```json
    "best_bug_classes": [
      "direct loss of user funds",
      "protocol insolvency",
      "bad debt creation",
      "unauthorized collateral withdrawal",
      "unauthorized collateral seizure",
      "permanent or long-term fund freeze",
      "liquidation bypass",
      "healthy-account liquidation",
      "offer replay or overfill",
      "gate or ratifier bypass",
      "credit/debt accounting corruption",
      "callback or multicall state corruption"
    ]
```
