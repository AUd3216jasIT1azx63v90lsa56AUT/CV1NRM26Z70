### Title
`mulDivDown` overflow in `liquidate()` health-check loop causes permanent DoS when oracle price exceeds `(2^256-1) / type(uint128).max` — (`File: src/Midnight.sol`)

### Summary
`UtilsLib.mulDivDown` is implemented as the plain Solidity expression `(x * y) / d`, which reverts on overflow under checked arithmetic (Solidity ≥0.8). Inside `liquidate()`, the expression `_collateral.mulDivDown(price, ORACLE_PRICE_SCALE)` multiplies a `uint128` collateral value by a `uint256` oracle price with no upper-bound guard. If `collateral * price > type(uint256).max`, the call reverts unconditionally, permanently blocking liquidation for every position in that market. The Certora `NoMultiplicationOverflow.spec` explicitly treats a bounded oracle price as an **assumption**, not an on-chain enforced invariant.

### Finding Description
**Code path:**

`liquidate()` → while-loop over `_collateralBitmap` → line 613:
```solidity
maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE)
               .mulDivDown(_collateralParam.lltv, WAD);
```
and line 615:
```solidity
_collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
```

`mulDivDown` / `mulDivUp` are both implemented as plain checked multiplication: [1](#0-0) 

`_collateral` is stored as `uint128` (max ≈ 3.4 × 10³⁸). `price` is an unconstrained `uint256` returned by the oracle. Overflow occurs when:

```
collateral * price > 2^256 - 1
```

With `collateral = type(uint128).max`, this triggers when `price > (2^256-1) / (2^128-1) ≈ 2^128 ≈ 3.4 × 10³⁸`, i.e., roughly `340 × ORACLE_PRICE_SCALE` (`1e36`). [2](#0-1) 

`touchMarket` validates `lltv` and `maxLif` but imposes **no upper bound on oracle prices**: [3](#0-2) 

The Certora overflow proof is conditional on a `boundedPrice` assumption that is never enforced on-chain: [4](#0-3) 

**Attacker-controlled inputs:**
- A market creator (unprivileged role) deploys an oracle contract whose `price()` returns a value above the overflow threshold.
- Creates a market referencing that oracle.
- Any borrower who supplies collateral and takes debt in that market becomes permanently unliquidatable.

**Exploit flow:**
1. Attacker deploys `MaliciousOracle` with `price()` returning `type(uint256).max`.
2. Attacker calls `touchMarket` / any entry point with a `Market` struct referencing `MaliciousOracle`. Market is created.
3. Borrower (attacker or victim) calls `supplyCollateral` + `take` to open a leveraged position.
4. Any call to `liquidate()` enters the while-loop at line 607, calls `IOracle(_collateralParam.oracle).price()` → returns `type(uint256).max`, then `_collateral * type(uint256).max` overflows → revert.
5. `liquidate()` is permanently blocked for all positions in this market.

**Why existing checks fail:**
- `touchMarket` validates only `lltv` and `maxLif`, not oracle price range.
- `mulDivDown`/`mulDivUp` have no 512-bit intermediate; they rely on the caller to guarantee no overflow.
- The Certora proof of no overflow is explicitly conditional on an off-chain integration assumption, not an on-chain invariant. [5](#0-4) 

### Impact Explanation
Any borrower in a market whose oracle returns a price above `(2^256-1) / type(uint128).max` cannot be liquidated. The `liquidate()` function reverts at the health-check loop before any state change, permanently freezing unhealthy positions. This violates the core invariant that unhealthy positions remain liquidatable and allows bad debt to accumulate without recourse.

### Likelihood Explanation
A market creator is an unprivileged role — anyone can call `touchMarket`. Deploying a one-line oracle returning `type(uint256).max` requires no privilege. The attacker can also be the borrower, making the attack entirely self-contained. The precondition (non-zero collateral in the bitmap) is trivially satisfied by any active borrower. The DoS is permanent and repeatable across any number of markets.

### Recommendation
Replace the plain-multiplication `mulDivDown`/`mulDivUp` with a 512-bit overflow-safe implementation (e.g., Solady's `FullMath.mulDiv`) so that `collateral * price` is computed in 512-bit space before dividing by `ORACLE_PRICE_SCALE`. Alternatively, enforce an on-chain oracle price cap in `touchMarket` (e.g., `require(IOracle(oracle).price() <= type(uint256).max / type(uint128).max)`) so that no market can be created with an oracle capable of producing an overflowing product.

### Proof of Concept
```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {Market, CollateralParams} from "src/interfaces/IMidnight.sol";
import {ORACLE_PRICE_SCALE} from "src/libraries/ConstantsLib.sol";

contract OverflowOracle {
    // Returns a price that causes collateral * price to overflow uint256.
    // Overflow threshold: price > (2^256-1) / type(uint128).max ≈ 2^128
    function price() external pure returns (uint256) {
        return type(uint256).max; // or any value > 2^128
    }
}

contract LiquidateOverflowDoSTest is Test {
    Midnight midnight;
    Market market;

    function setUp() public {
        midnight = new Midnight();
        market.loanToken = address(new ERC20(...));
        market.maturity = block.timestamp + 30 days;
        market.collateralParams.push(CollateralParams({
            token: address(new ERC20(...)),
            lltv: 0.77e18,
            maxLif: ...,
            oracle: address(new OverflowOracle())
        }));
    }

    function testLiquidateOverflowDoS() public {
        // 1. Create market (touchMarket succeeds — no price check).
        // 2. Supply collateral (any non-zero amount, e.g. 1e18).
        // 3. Take debt to open a position.
        // 4. Attempt liquidate — must revert with arithmetic overflow.
        vm.expectRevert(stdError.arithmeticError);
        midnight.liquidate(market, 0, 0, 0, borrower, true, address(this), address(0), "");
        // Assert: liquidation is permanently blocked.
    }

    // Fuzz variant: assert liquidate always reverts for any collateral > 0
    // when oracle returns price > type(uint256).max / type(uint128).max.
    function testFuzzLiquidateOverflow(uint128 collateral) public {
        vm.assume(collateral > 0);
        // supply `collateral`, open debt, set oracle to overflow price
        vm.expectRevert(stdError.arithmeticError);
        midnight.liquidate(market, 0, 0, 0, borrower, true, address(this), address(0), "");
    }
}
```

**Expected assertion:** `liquidate()` reverts with `Panic(0x11)` (arithmetic overflow) for any non-zero collateral when the oracle returns a price above `(2^256-1) / type(uint128).max`.

### Citations

**File:** src/libraries/UtilsLib.sol (L29-36)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }

    /// @dev Returns (x * y) / d rounded up.
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
```

**File:** src/libraries/ConstantsLib.sol (L9-9)
```text
uint256 constant ORACLE_PRICE_SCALE = 1e36;
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

**File:** certora/specs/NoMultiplicationOverflow.spec (L45-50)
```text
// Bound every storage collateral (uint128) * oracle price product.
function boundedPrice(address oracle) returns uint256 {
    uint256 price;
    require to_mathint(price) * max_uint128 + ORACLE_PRICE_SCALE() - 1 <= max_uint256, "same as assuming that collateral * price <= uint256 with mulDivUp rounding headroom";
    return price;
}
```
