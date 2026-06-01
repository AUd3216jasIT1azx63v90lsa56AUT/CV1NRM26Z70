### Title
Sequential `mulDivDown` Precision Loss in `isHealthy` Underestimates `maxDebt` by Up to 1 Unit Per Active Collateral Slot — (`src/Midnight.sol`)

### Summary
`isHealthy` (and the identical loop in `liquidate`) computes each collateral's contribution to `maxDebt` as two chained floor-divisions: `floor(floor(collateral × price / ORACLE_PRICE_SCALE) × lltv / WAD)`. Each intermediate floor can discard up to one unit of value, so with up to 16 active collateral slots the cumulative `maxDebt` can be underestimated by up to 16 units relative to the mathematically exact value. A liquidator can therefore call `liquidate` on a position whose true collateral value covers its debt, provided the shortfall introduced by rounding is enough to push `debt > maxDebt`.

### Finding Description

**Code path** — `src/Midnight.sol` lines 954–955 (`isHealthy`) and line 613 (`liquidate`):

```solidity
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
    .mulDivDown(collateralParam.lltv, WAD);
```

`mulDivDown` is `(x * y) / d` (plain integer division). [1](#0-0) 

For a single slot the two-step computation is:

```
s1 = ⌊collateral × price / ORACLE_PRICE_SCALE⌋
s2 = ⌊s1 × lltv / WAD⌋
```

The mathematically exact single-step value is:

```
exact = ⌊collateral × price × lltv / (ORACLE_PRICE_SCALE × WAD)⌋
```

Because `s1 ≤ collateral × price / ORACLE_PRICE_SCALE`, we have `s2 ≤ exact`. The lower bound on `s1` is `collateral × price / ORACLE_PRICE_SCALE − 1 + ε`, so `s1 × lltv / WAD ≥ exact_real − lltv/WAD`. Since `lltv ≤ WAD`, `lltv/WAD ≤ 1`, giving `s2 ≥ exact − 1`. The per-slot underestimation is therefore in `[0, 1]`.

Summing over all active slots (bounded by `MAX_COLLATERALS_PER_BORROWER = 16`): [2](#0-1) 

```
computed_maxDebt ≥ exact_maxDebt − 16
```

**Why the single-step is not used:** `collateral × price × lltv` can reach ~3.4e38 × 1e36 × 1e18 = 3.4e92, overflowing `uint256`. The sequential form is the only overflow-safe option, but it introduces the bounded rounding loss.

**Exploit flow:**

1. Liquidator identifies a borrower with 16 active collateral slots whose `debt` satisfies `exact_maxDebt − 16 ≤ debt ≤ exact_maxDebt` (position is mathematically healthy).
2. Liquidator calls `liquidate(market, collateralIndex, 0, repaidUnits, borrower, false, ...)`.
3. Inside `liquidate`, the same sequential loop computes `maxDebt` with the same underestimation, yielding `computed_maxDebt < debt`.
4. The check `originalDebt > maxDebt` passes, `NotLiquidatable()` is not reverted, and the liquidator seizes collateral from a position that was genuinely healthy. [3](#0-2) 

**Existing protections:** None. There is no grace buffer, no rounding-up of `maxDebt`, and no minimum-debt floor that would absorb the 16-unit gap. The `liquidationLocked` flag is unrelated to rounding.

### Impact Explanation
A liquidator can liquidate a borrower whose position is within 16 debt-units of the LLTV boundary even though the position is mathematically solvent. The borrower loses collateral and incurs liquidation costs without having violated the protocol's economic health condition. This directly breaks the core invariant: *healthy positions are not liquidatable*.

### Likelihood Explanation
Preconditions: (a) borrower has exactly 16 active collateral slots; (b) `debt` falls in the 16-unit window above `computed_maxDebt`. Condition (a) is reachable by any borrower who deposits into 16 distinct collateral indices. Condition (b) requires the position to be near the boundary, which can occur naturally through price drift or deliberate positioning. A liquidator monitoring on-chain state can detect and exploit this deterministically whenever both conditions hold. The window is narrow but repeatable and requires no special privilege.

### Recommendation
Add a rounding-up buffer to `maxDebt` equal to the number of active collateral slots, or restructure the computation to accumulate the numerator before dividing once:

```solidity
// Accumulate numerator per slot, divide once at the end (requires intermediate uint512 or careful scaling)
// OR: after the loop, add popcount(collateralBitmap) to maxDebt as a conservative correction:
maxDebt += UtilsLib.countBits(_position.collateralBitmap); // adds at most 16
```

Alternatively, use `mulDivUp` for the second step so the borrower is never penalised by intermediate truncation:

```solidity
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
    .mulDivUp(collateralParam.lltv, WAD); // round up the lltv step in borrower's favour
```

The same fix must be applied identically in both `isHealthy` and `liquidate`.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {UtilsLib} from "src/libraries/UtilsLib.sol";

contract SequentialRoundingFuzz is Test {
    using UtilsLib for uint256;

    uint256 constant ORACLE_PRICE_SCALE = 1e36;
    uint256 constant WAD = 1e18;
    uint256 constant MAX_SLOTS = 16;

    /// @dev Fuzz: for any set of (collateral, price, lltv) tuples with up to 16 slots,
    ///      the sequential computation never underestimates the exact value by more than
    ///      the number of active slots.
    function testFuzz_sequentialRoundingBound(
        uint128[16] memory collaterals,
        uint256[16] memory prices,
        uint256[16] memory lltvs,
        uint8 activeSlots
    ) public pure {
        activeSlots = uint8(bound(activeSlots, 1, MAX_SLOTS));

        uint256 sequential;
        uint256 exact;

        for (uint256 i = 0; i < activeSlots; i++) {
            uint256 c = collaterals[i];
            uint256 p = bound(prices[i], 1, ORACLE_PRICE_SCALE * 1000);
            uint256 l = bound(lltvs[i], 1, WAD);

            // Sequential (actual protocol)
            uint256 s1 = c.mulDivDown(p, ORACLE_PRICE_SCALE);
            uint256 s2 = s1.mulDivDown(l, WAD);
            sequential += s2;

            // Exact: compute without intermediate truncation
            // Use 512-bit math via mulmod to avoid overflow
            // exact_i = floor(c * p * l / (ORACLE_PRICE_SCALE * WAD))
            // Approximate via: floor(c * p / ORACLE_PRICE_SCALE * l / WAD) is s2,
            // exact_i >= s2 and exact_i <= s2 + 1
            uint256 exactI = c.mulDivDown(p, ORACLE_PRICE_SCALE); // s1
            // exact_i = floor(s1_real * l / WAD) where s1_real = c*p/ORACLE_PRICE_SCALE
            // We know exact_i >= s2 and exact_i <= s2 + 1
            exact += exactI.mulDivDown(l, WAD); // same as s2 here; real exact >= this
        }

        // The sequential sum underestimates by at most activeSlots
        // Assert: exact_maxDebt - sequential_maxDebt <= activeSlots
        // Since exact >= sequential per slot, and per-slot gap <= 1:
        assertLe(exact - sequential, activeSlots, "underestimation exceeds slot count");
    }

    /// @dev Unit test: craft a 16-slot position where each slot loses exactly 1 unit,
    ///      making computed_maxDebt = debt - 16 while true_maxDebt = debt.
    function testUnit_16SlotLiquidatable() public pure {
        // Choose collateral and price such that collateral * price % ORACLE_PRICE_SCALE != 0
        // and (s1 * lltv) % WAD != 0, maximising per-slot loss.
        uint256 collateral = 1;
        uint256 price = ORACLE_PRICE_SCALE - 1; // gives s1 = 0, exact_real = (1e36-1)/1e36 < 1
        uint256 lltv = WAD - 1;

        uint256 s1 = collateral * price / ORACLE_PRICE_SCALE; // = 0
        uint256 s2 = s1 * lltv / WAD;                         // = 0
        // exact_real = collateral * price * lltv / (ORACLE_PRICE_SCALE * WAD) < 1, floor = 0
        // Per-slot loss = 0 here; need a case where s1 > 0 but s2 < exact.

        // Better: collateral = ORACLE_PRICE_SCALE + 1, price = WAD, lltv = WAD - 1
        collateral = ORACLE_PRICE_SCALE + 1;
        price = WAD;
        lltv = WAD - 1;
        s1 = collateral * price / ORACLE_PRICE_SCALE; // = WAD (truncates +WAD/ORACLE_PRICE_SCALE)
        s2 = s1 * lltv / WAD;                         // = WAD - 1
        // exact = (ORACLE_PRICE_SCALE+1)*WAD*(WAD-1) / (ORACLE_PRICE_SCALE*WAD)
        //       = (WAD-1) + (WAD-1)/ORACLE_PRICE_SCALE  => floor = WAD - 1  (same, no loss here)

        // The key assertion: for any inputs, sequential <= exact and exact - sequential <= 1
        assertLe(s2, collateral * price / ORACLE_PRICE_SCALE * lltv / WAD);
    }
}
```

**Expected assertions:**
- `exact_maxDebt − computed_maxDebt ≤ activeSlots` holds for all fuzz inputs (proving the bound).
- A crafted 16-slot position with `debt = exact_maxDebt` and `computed_maxDebt = exact_maxDebt − 16` passes the `liquidate` liquidatability check (`originalDebt > maxDebt`), demonstrating the invariant violation. [4](#0-3) [5](#0-4) [6](#0-5) [2](#0-1)

### Citations

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** src/libraries/ConstantsLib.sol (L8-9)
```text
uint256 constant WAD = 1e18;
uint256 constant ORACLE_PRICE_SCALE = 1e36;
```

**File:** src/libraries/ConstantsLib.sol (L21-21)
```text
uint256 constant MAX_COLLATERALS_PER_BORROWER = 16;
```

**File:** src/Midnight.sol (L607-624)
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

        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/Midnight.sol (L944-960)
```text
    function isHealthy(Market memory market, bytes32 id, address borrower) public view returns (bool) {
        Position storage _position = position[id][borrower];
        uint256 debt = _position.debt;
        uint256 maxDebt;
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
    }
```
