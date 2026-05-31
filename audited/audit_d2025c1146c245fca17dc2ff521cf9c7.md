### Title
Market-Creator-Controlled Oracle Suppresses Bad-Debt Realization via Price Inflation - (File: src/Midnight.sol)

### Summary
`touchMarket` is permissionless and performs no validation on the oracle address beyond structural checks on `lltv` and `maxLif`. A market creator can deploy a stateful oracle that returns a normal price during ordinary operation but returns an inflated price during liquidation. Because `badDebt` is computed with `zeroFloorSub` against the oracle-quoted collateral value divided by `maxLif`, a sufficiently inflated price drives `badDebt` to zero, skipping the `lossFactor` update and leaving lenders' credit unslashed while the market is actually insolvent.

### Finding Description

**Market creation — no oracle validation:**

`touchMarket` validates only `lltv` and `maxLif`; the `oracle` field in `CollateralParams` is accepted as any address. [1](#0-0) 

**`badDebt` computation in `liquidate`:**

```solidity
badDebt = badDebt.zeroFloorSub(
    _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
);
``` [2](#0-1) 

Let `V' = collateral * P' / ORACLE_PRICE_SCALE` (collateral value at inflated price `P'`).

- `badDebt = 0` when `V' * WAD / maxLif >= debt`, i.e. `V' >= debt * maxLif / WAD`.
- Position remains liquidatable (pre-maturity) when `debt > V' * lltv / WAD`, i.e. `V' < debt * WAD / lltv`.

The Certora spec formally proves `lltv * maxLif <= WAD^2`: [3](#0-2) 

This guarantees the interval `[debt * maxLif / WAD, debt * WAD / lltv)` is non-empty for every valid `(lltv, maxLif)` pair. A concrete example with `lltv = 0.77e18`, `cursor = 0.25e18` gives `maxLif ≈ 1.061e18`, so the window is `[debt * 1.061, debt * 1.299)`.

**Liquidatability check uses the same oracle price:** [4](#0-3) 

In **post-maturity mode** (`postMaturityMode = true`) the check is purely `block.timestamp > market.maturity` — the oracle price does not affect liquidatability at all, so the attacker only needs `V' >= debt * maxLif / WAD` with no upper bound.

**`lossFactor` update is gated on `badDebt > 0`:** [5](#0-4) 

When `badDebt = 0`, none of `_position.debt`, `_marketState.lossFactor`, `_marketState.totalUnits`, or `_marketState.continuousFeeCredit` are adjusted. Lenders' credit is never slashed.

**Exploit flow:**
1. Attacker deploys `ManipulableOracle` with a `setInflated(bool)` toggle.
2. Attacker calls `touchMarket` with `oracle = address(ManipulableOracle)` — passes all checks.
3. Lenders supply credit; borrower (attacker or accomplice) borrows and lets collateral value fall below debt (true bad debt).
4. Attacker calls `setInflated(true)` so the oracle returns `P'` such that `V' ∈ [debt * maxLif / WAD, debt * WAD / lltv)`.
5. Any liquidator calls `liquidate(... postMaturityMode=true ...)` — position is liquidatable, but `badDebt = 0`.
6. `lossFactor` is not updated; lenders' credit is not slashed; `totalUnits` is not reduced.
7. Lenders believe their credit is redeemable at face value; the market is insolvent.

### Impact Explanation
Lenders' credit is not slashed despite real bad debt existing. The `lossFactor` and `totalUnits` remain at pre-insolvency values, so every subsequent `withdraw` or credit redemption draws on assets that do not exist. The protocol's accounting invariant — every credit unit has a matching debt unit or valid settled/loss state — is broken silently, with no on-chain signal to lenders.

### Likelihood Explanation
Market creation is fully permissionless; no whitelist or oracle registry exists. The attacker only needs to deploy a two-state oracle contract (normal / inflated) and create a market referencing it. The attack is repeatable across any number of markets and maturities. The only precondition is that the market attracts lenders before the attacker triggers the inflation, which is achievable by operating normally until sufficient TVL is deposited.

### Recommendation
Introduce an oracle registry or whitelist enforced in `touchMarket`, so only protocol-approved oracle addresses can be used. Alternatively, add a secondary bad-debt check that uses a time-weighted or manipulation-resistant price source, or require that `badDebt` be computed independently of the same oracle used for liquidatability. At minimum, document that oracle trust is fully delegated to the market creator and that lenders must vet the oracle before supplying.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {Market, CollateralParams} from "src/interfaces/IMidnight.sol";
import {WAD, ORACLE_PRICE_SCALE} from "src/libraries/ConstantsLib.sol";

contract ManipulableOracle {
    uint256 public normalPrice  = 1e36;   // 1:1
    uint256 public inflatedPrice;
    bool    public inflated;
    function price() external view returns (uint256) {
        return inflated ? inflatedPrice : normalPrice;
    }
    function setInflated(bool v, uint256 p) external { inflated = v; inflatedPrice = p; }
}

contract BadDebtSuppressionTest is Test {
    // Setup: permissionless market with ManipulableOracle,
    //        lender supplies credit, borrower borrows 100 units,
    //        collateral drops to 50 (true bad debt = 50).
    //
    // Step 1: attacker sets oracle to inflated price P' such that
    //         collateral * P' / ORACLE_PRICE_SCALE / maxLif >= 100
    //         AND collateral * P' / ORACLE_PRICE_SCALE * lltv / WAD < 100
    //
    // Step 2: call liquidate(postMaturityMode=true, seizedAssets=0, repaidUnits=0)
    //         (pure bad-debt realization call, zero tokens transferred)
    //
    // Assertions:
    //   assertEq(badDebtEmitted, 0,          "bad debt must be zero");
    //   assertEq(lossFactorAfter, lossFactorBefore, "lossFactor must not change");
    //   assertEq(totalUnitsAfter, totalUnitsBefore, "totalUnits must not change");
    //   assertGt(trueShortfall, 0,            "true shortfall exists");
    //
    // Invariant fuzz: for all oracle prices in [debt*maxLif/WAD, debt*WAD/lltv),
    //   badDebt == 0 while position is liquidatable post-maturity.
}
```

Expected result: all assertions pass, demonstrating that a market-creator-controlled oracle can suppress bad-debt realization indefinitely, leaving lenders with phantom credit.

### Citations

**File:** src/Midnight.sol (L614-616)
```text
            badDebt = badDebt.zeroFloorSub(
                _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
            );
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/Midnight.sol (L626-641)
```text
        if (badDebt > 0) {
            // forge-lint: disable-next-item(unsafe-typecast) as badDebt <= _position.debt
            _position.debt -= uint128(badDebt);
            uint256 _totalUnits = _marketState.totalUnits;
            uint256 _lossFactor = _marketState.lossFactor;
            _marketState.lossFactor = UtilsLib.toUint128(
                type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
            );
            _marketState.totalUnits -= UtilsLib.toUint128(badDebt);
            _marketState.continuousFeeCredit = _lossFactor < type(uint128).max
                ? UtilsLib.toUint128(
                    _marketState.continuousFeeCredit
                        .mulDivDown(type(uint128).max - _marketState.lossFactor, type(uint128).max - _lossFactor)
                )
                : 0;
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

**File:** certora/specs/ExactMath.spec (L12-16)
```text
rule lifTimesLltvIsLessThanOrEqualToOne(uint256 lltv, uint256 cursor) {
    require lltv <= WAD(), "see rule createdMarketsHaveLltvLessThanOrEqualToOne";
    require cursor < WAD(), "see the definition of LIQUIDATION_CURSOR_LOW and LIQUIDATION_CURSOR_HIGH";
    assert lltv * maxLif(lltv, cursor) <= WAD() * WAD();
}
```
