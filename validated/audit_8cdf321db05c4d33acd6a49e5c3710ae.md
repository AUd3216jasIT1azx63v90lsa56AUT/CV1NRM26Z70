### Title
Zero Oracle Price Allows Free Collateral Seizure in `liquidate()` — (File: src/Midnight.sol)

---

### Summary

The `liquidate()` function in `src/Midnight.sol` fetches the oracle price for the liquidated collateral but never validates that it is greater than zero before using it in the seized-assets computation path. When the oracle returns `0`, the computed `repaidUnits` is also `0`, allowing an attacker to seize a borrower's entire collateral without repaying any debt. The borrower's debt is simultaneously wiped as bad debt and socialized to lenders.

---

### Finding Description

**Vulnerability class:** Pricing / oracle validation — missing zero-price check (direct analog to the external Gearbox report).

In `liquidate()`, the oracle price for each activated collateral is fetched in a loop:

```solidity
// src/Midnight.sol lines 607–618
while (_collateralBitmap != 0) {
    uint256 i = UtilsLib.msb(_collateralBitmap);
    CollateralParams memory _collateralParam = market.collateralParams[i];
    uint256 price = IOracle(_collateralParam.oracle).price();   // ← no > 0 check
    if (i == collateralIndex) liquidatedCollatPrice = price;    // ← can be 0
    ...
}
``` [1](#0-0) 

When `seizedAssets > 0` is passed as input, `repaidUnits` is derived from `liquidatedCollatPrice`:

```solidity
// src/Midnight.sol lines 649–651
if (seizedAssets > 0) {
    repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
}
``` [2](#0-1) 

`mulDivUp` is defined as `(x * y + (d - 1)) / d`: [3](#0-2) 

When `liquidatedCollatPrice == 0`:
- `mulDivUp(seizedAssets, 0, ORACLE_PRICE_SCALE)` = `(0 + ORACLE_PRICE_SCALE - 1) / ORACLE_PRICE_SCALE` = **0**
- `mulDivUp(0, WAD, lif)` = `(0 + lif - 1) / lif` = **0**

So `repaidUnits = 0`. No revert occurs. The attacker passes `seizedAssets > 0, repaidUnits = 0` (satisfying the `atMostOneNonZero` check), and the execution continues:

```solidity
// src/Midnight.sol lines 670–676
uint128 newCollateral = _position.collateral[collateralIndex] - UtilsLib.toUint128(seizedAssets);
_position.collateral[collateralIndex] = newCollateral;
...
_marketState.withdrawable += UtilsLib.toUint128(repaidUnits);  // += 0
_position.debt -= UtilsLib.toUint128(repaidUnits);             // -= 0
``` [4](#0-3) 

The attacker receives `seizedAssets` collateral tokens and transfers **0** loan tokens.

**Why the borrower is liquidatable when oracle = 0:**

When the oracle returns 0, `isHealthy()` computes `maxDebt = 0` (zero contribution from all collaterals), making any borrower with debt immediately unhealthy and liquidatable: [5](#0-4) 

This is confirmed by the Certora spec: [6](#0-5) 

**The bad debt path runs first:** With oracle = 0, `badDebt = originalDebt` (all debt is bad debt). The debt is wiped and socialized to lenders before the seized-assets block executes. The attacker then seizes collateral for free.

**Gap in the Certora spec:** The spec verifies `oracleZeroCausesLiquidateWithRepaidRevert` (repaid input reverts) but has **no corresponding rule** for the seized-assets input path, leaving this path unverified: [7](#0-6) 

The LIVENESS section in `Midnight.sol` also only documents the repaid-input revert, not the seized-assets free-seizure case: [8](#0-7) 

---

### Impact Explanation

An attacker can:
1. Identify a market whose collateral oracle can return `0` (oracle failure, manipulation, or flash-loan attack — explicitly not excluded by `SECURITY.md`).
2. Call `liquidate(market, collateralIndex, seizedAssets, 0, borrower, false, receiver, address(0), "")`.
3. Receive the borrower's full collateral balance while paying **zero** loan tokens.
4. The borrower's debt is simultaneously socialized as bad debt, causing lenders to lose funds proportionally.

Concrete outcome: attacker gains collateral for free; lenders absorb the full debt loss; borrower loses both collateral and has their debt wiped at lenders' expense.

---

### Likelihood Explanation

The `SECURITY.md` explicitly states oracle manipulation/flash-loan attacks are **not** excluded: [9](#0-8) 

Any oracle that can be transiently driven to `0` (e.g., a TWAP or spot oracle on a low-liquidity pool) is sufficient. The attacker requires no privileged access — only the ability to call `liquidate()` as a normal user. The market creation flow (`touchMarket`) performs no oracle price validation, so markets with vulnerable oracles can be created permissionlessly. [10](#0-9) 

---

### Recommendation

Add an explicit zero-price guard in `liquidate()` before the seized-assets computation, analogous to the fix recommended in the external Gearbox report:

```solidity
// After: if (i == collateralIndex) liquidatedCollatPrice = price;
// Add before the seized-assets block:
if (repaidUnits > 0 || seizedAssets > 0) {
    require(liquidatedCollatPrice > 0, ZeroOraclePrice());
    ...
}
```

Alternatively, add the check immediately after `liquidatedCollatPrice` is assigned in the loop. This mirrors the external report's recommendation to validate `getLastPrice() > 0` rather than merely checking for pair existence.

---

### Proof of Concept

**Setup:**
- Market with one collateral token, oracle initially returning `1e36`.
- Borrower supplies 100 collateral tokens, borrows 80 units of debt.

**Attack:**
1. Attacker manipulates (or waits for) the oracle to return `0`.
2. `isHealthy()` now returns `false` (maxDebt = 0 < 80 debt). Borrower is liquidatable.
3. Attacker calls:
   ```solidity
   midnight.liquidate(market, 0, 100e18, 0, borrower, false, attacker, address(0), "");
   ```
4. Inside `liquidate()`:
   - `liquidatedCollatPrice = 0`
   - `badDebt = 80` → debt wiped, lenders lose 80 units
   - `repaidUnits = mulDivUp(100e18, 0, 1e36).mulDivUp(WAD, lif) = 0`
   - `_position.collateral[0] -= 100e18` → borrower loses all collateral
   - `SafeTransferLib.safeTransfer(collateralToken, attacker, 100e18)` → attacker receives 100 collateral tokens
   - `SafeTransferLib.safeTransferFrom(loanToken, attacker, address(this), 0)` → attacker pays nothing

**Result:** Attacker gains 100 collateral tokens for free. Lenders lose 80 units of loan token. Borrower loses all collateral.

### Citations

**File:** src/Midnight.sol (L146-146)
```text
/// @dev If the liquidated collateral oracle returns 0 on price, liquidate with repaid input reverts.
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

**File:** src/Midnight.sol (L649-651)
```text
            if (seizedAssets > 0) {
                repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
            } else {
```

**File:** src/Midnight.sol (L670-676)
```text
            uint128 newCollateral = _position.collateral[collateralIndex] - UtilsLib.toUint128(seizedAssets);
            _position.collateral[collateralIndex] = newCollateral;
            if (newCollateral == 0 && seizedAssets > 0) {
                _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
            }
            _marketState.withdrawable += UtilsLib.toUint128(repaidUnits);
            _position.debt -= UtilsLib.toUint128(repaidUnits);
```

**File:** src/Midnight.sol (L755-791)
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

            MarketState storage _marketState = marketState[id];
            _marketState.tickSpacing = DEFAULT_TICK_SPACING;
            uint16[7] memory _defaultSettlementFeeCbp = defaultSettlementFeeCbp[market.loanToken];
            _marketState.settlementFeeCbp0 = _defaultSettlementFeeCbp[0];
            _marketState.settlementFeeCbp1 = _defaultSettlementFeeCbp[1];
            _marketState.settlementFeeCbp2 = _defaultSettlementFeeCbp[2];
            _marketState.settlementFeeCbp3 = _defaultSettlementFeeCbp[3];
            _marketState.settlementFeeCbp4 = _defaultSettlementFeeCbp[4];
            _marketState.settlementFeeCbp5 = _defaultSettlementFeeCbp[5];
            _marketState.settlementFeeCbp6 = _defaultSettlementFeeCbp[6];
            _marketState.continuousFee = defaultContinuousFee[market.loanToken];
            IdLib.storeInCode(market, INITIAL_CHAIN_ID);

            emit EventsLib.MarketCreated(market, id);
        }
        return id;
    }
```

**File:** src/Midnight.sol (L948-959)
```text
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

**File:** src/libraries/UtilsLib.sol (L34-36)
```text
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
```

**File:** certora/specs/Reverts.spec (L245-253)
```text
/// If liquidated collateral oracle returns 0 on price, liquidate with repaid input reverts.
rule oracleZeroCausesLiquidateWithRepaidRevert(env e, Midnight.Market market, uint256 collateralIndex, uint256 repaidUnits, address borrower, address receiver, address callback, bytes data, bool postMaturityMode) {
    require singleZeroOracle == market.collateralParams[collateralIndex].oracle, "oracle returns zero";
    require repaidUnits > 0, "using repaid units as input";

    liquidate@withrevert(e, market, collateralIndex, 0, repaidUnits, borrower, postMaturityMode, receiver, callback, data);

    assert lastReverted;
}
```

**File:** certora/specs/Reverts.spec (L255-265)
```text
/// If all oracles return 0 and the borrower has debt, isHealthy returns false.
rule oracleZeroCausesIsHealthyReturnFalse(env e, Midnight.Market market, address borrower) {
    require forceOracleReturnZero, "all oracles return zero";

    bytes32 id = summaryToId(market);
    require collateralBitmap(id, borrower) != 0, "borrower has activated collaterals";

    bool healthy = isHealthy(e, market, id, borrower);

    assert debtOf(id, borrower) > 0 => !healthy;
}
```

**File:** SECURITY.md (L26-26)
```markdown
Note: This does not exclude oracle manipulation/flash-loan attacks.
```
