Audit Report

## Title
Borrower-Deployed Reverting Oracle Permanently Freezes Liquidation via Unconditional Bitmap Oracle Loop - (File: src/Midnight.sol)

## Summary
The `liquidate` function unconditionally calls `IOracle.price()` on every collateral in a borrower's bitmap before performing the health check, with no error handling. Because market creation is permissionless and `supplyCollateral` never calls the oracle, any borrower can activate a collateral slot backed by a self-deployed reverting oracle, making their position permanently unliquidatable and enabling unchecked bad debt accumulation.

## Finding Description

**Root cause:** In `src/Midnight.sol` lines 607–618, the `liquidate` while loop calls `IOracle(_collateralParam.oracle).price()` for every bit set in `_position.collateralBitmap` with no try/catch or error handling. [1](#0-0) 

If any single oracle reverts, the entire transaction reverts before the `NotLiquidatable` check at lines 620–624 is ever reached. [2](#0-1) 

**Why the attacker can reach this state:**

1. `touchMarket` (lines 755–791) is permissionless and validates only collateral token sort order, allowed LLTV tiers, and `maxLif` values. No oracle liveness check exists. [3](#0-2) 

2. `supplyCollateral` (lines 524–546) only sets the bitmap bit and transfers the token — no oracle is called, so a reverting oracle is silently activated. [4](#0-3) 

3. The protocol explicitly documents this gap at lines 34–36: [5](#0-4) 

And again in the LIVENESS section at lines 142–145: [6](#0-5) 

**Exploit flow:**

1. Attacker deploys a contract whose `price()` always reverts. The existing `test/helpers/RevertingOracle.sol` is exactly this — it has a `stopOracle()` function that makes `price()` revert. [7](#0-6) 

2. Attacker calls `touchMarket` with one collateral slot pointing to the reverting oracle. Market creation succeeds because no oracle liveness check exists.
3. Attacker calls `supplyCollateral` for that slot. Bitmap bit is set; no oracle call occurs.
4. Attacker borrows by taking a sell offer, creating debt.
5. Position becomes unhealthy (e.g., other oracle prices drop).
6. Any liquidator calls `liquidate`. The while loop reaches the reverting oracle slot, calls `price()`, which reverts. The entire `liquidate` call reverts. This is permanent.

**Why existing checks fail:**

- The `liquidatorGate` check (lines 597–600) only gates *who* can liquidate, not whether the oracle loop can revert. [8](#0-7) 

- `NotBorrower` and `InconsistentInput` guards fire before the loop but cannot prevent oracle reversion inside it.
- The Certora rule `oracleRevertCausesLiquidateRevert` (certora/specs/Reverts.spec lines 183–193) formally proves this revert propagation, confirming no mitigation exists in the current code. [9](#0-8) 

- `repay` does not call oracles, so the borrower can voluntarily repay, but a liquidator cannot force repayment.

## Impact Explanation

Any borrower who activates a collateral slot backed by a reverting oracle — achievable unilaterally via permissionless market creation — permanently prevents liquidation of their position. The core protocol invariant that unhealthy positions remain liquidatable is broken. Bad debt accumulates with no mechanism for liquidators to recover it, directly threatening lender solvency. This maps to "Permanent lock, freeze, or unrecoverable corruption of user/project state" per RESEARCHER.md. [10](#0-9) 

## Likelihood Explanation

All preconditions are reachable by any EOA with no privileged access:
- Market creation is permissionless (zero privilege required).
- No minimum collateral amount is enforced for bitmap activation.
- The attacker controls the oracle address at market creation time.

The attack is self-contained (create market → supply → borrow → freeze), repeatable across any number of markets, and permanent once the reverting oracle is activated in the bitmap. The `RevertingOracle` test helper already exists in the repo, confirming the developers modeled this exact scenario. [11](#0-10) 

## Recommendation

Wrap the oracle call inside the `liquidate` while loop in a try/catch. If an oracle reverts, treat the price as 0 (or skip that collateral's contribution to `maxDebt`), allowing liquidation to proceed. Alternatively, add an oracle liveness check in `touchMarket` or `supplyCollateral` to prevent activation of reverting oracles. A third option is to allow liquidators to pass a subset bitmap of collaterals to price, skipping reverting ones, though this changes the health-check semantics.

## Proof of Concept

```solidity
// 1. Deploy reverting oracle
RevertingOracle oracle = new RevertingOracle();

// 2. Create market with one collateral slot pointing to the reverting oracle
Market memory market = ...; // collateralParams[0].oracle = address(oracle)
midnight.touchMarket(market);

// 3. Supply collateral (oracle NOT called here — bitmap bit set silently)
midnight.supplyCollateral(market, 0, 1e18, attacker);

// 4. Borrow by taking a sell offer
midnight.take(...); // attacker now has debt

// 5. Stop the oracle (or deploy it already stopped)
oracle.stopOracle();

// 6. Attempt liquidation — reverts unconditionally
midnight.liquidate(market, 0, 0, 1, attacker, false, liquidator, address(0), "");
// ↑ Reverts at line 610: IOracle(oracle).price() reverts
```

The Certora rule `oracleRevertCausesLiquidateRevert` in `certora/specs/Reverts.spec` lines 183–193 is a formal proof of this exact revert path and can serve as a machine-checked PoC. [12](#0-11)

### Citations

**File:** src/Midnight.sol (L34-36)
```text
/// @dev Liquidation reverts if any of the activated collaterals' oracle reverts (see LIVENESS).
/// @dev Note that a borrower can activate a collateral once its oracle is reverting because the oracle is not called in
/// supplyCollateral.
```

**File:** src/Midnight.sol (L142-145)
```text
/// LIVENESS
/// @dev If an activated collateral oracle reverts on price, liquidate reverts.
/// @dev If an activated collateral oracle reverts on price, isHealthy, withdrawCollateral and take revert when the user
/// (seller for take) has non-zero debt.
```

**File:** src/Midnight.sol (L524-546)
```text
    function supplyCollateral(Market memory market, uint256 collateralIndex, uint256 assets, address onBehalf)
        external
    {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        address collateralToken = market.collateralParams[collateralIndex].token;

        Position storage _position = position[id][onBehalf];
        uint256 oldCollateral = _position.collateral[collateralIndex];
        _position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets);

        if (oldCollateral == 0 && assets > 0) {
            uint128 newCollateralBitmap = _position.collateralBitmap.setBit(collateralIndex);
            _position.collateralBitmap = newCollateralBitmap;
            require(
                UtilsLib.countBits(newCollateralBitmap) <= MAX_COLLATERALS_PER_BORROWER, TooManyActivatedCollaterals()
            );
        }

        emit EventsLib.SupplyCollateral(msg.sender, id, collateralToken, assets, onBehalf);

        SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
    }
```

**File:** src/Midnight.sol (L597-600)
```text
        require(
            market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
            LiquidatorGatedFromLiquidating()
        );
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

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/Midnight.sol (L755-773)
```text
    function touchMarket(Market memory market) public returns (bytes32) {
        bytes32 id = toId(market);
        if (marketState[id].tickSpacing == 0) {
            require(market.maturity <= block.timestamp + 100 * 365 days, MaturityTooFar());
            require(market.collateralParams.length > 0, NoCollateralParams());
            require(market.collateralParams.length <= MAX_COLLATERALS, TooManyCollateralParams());
            address previousCollateralToken;
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

**File:** certora/specs/Reverts.spec (L182-193)
```text
/// If any activated collateral oracle reverts on price, liquidate reverts.
rule oracleRevertCausesLiquidateRevert(env e, Midnight.Market market, uint256 collateralIndex, uint256 seizedAssets, uint256 repaidUnits, address borrower, address receiver, address callback, bytes data, uint256 revertingCollateralIndex, bool postMaturityMode) {
    require singleRevertingOracle == market.collateralParams[revertingCollateralIndex].oracle, "oracle is reverting";

    bytes32 id = summaryToId(market);
    uint128 bitmap = collateralBitmap(id, borrower);
    require summaryGetBit(bitmap, revertingCollateralIndex), "revertingCollateralIndex is activated";

    liquidate@withrevert(e, market, collateralIndex, seizedAssets, repaidUnits, borrower, postMaturityMode, receiver, callback, data);

    assert lastReverted;
}
```

**File:** RESEARCHER.md (L14-14)
```markdown
- Permanent lock, freeze, or unrecoverable corruption of user/project state.
```
