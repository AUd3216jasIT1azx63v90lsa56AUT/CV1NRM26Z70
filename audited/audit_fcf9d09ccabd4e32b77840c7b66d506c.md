I have all the code I need. Let me work through the arithmetic precisely.

### Title
Rounding in lossFactor update prematurely saturates market when `_lossFactor = type(uint128).max - 1`, blocking all future takes via `MarketLossFactorMaxedOut` - (File: src/Midnight.sol)

### Summary
When `_marketState.lossFactor` equals `type(uint128).max - 1`, the inner `mulDivDown` in the lossFactor update formula evaluates to `0` for any `badDebt >= 1`, regardless of whether `badDebt < totalUnits`. This causes the new lossFactor to be set to `type(uint128).max`, prematurely saturating the market and permanently blocking all future `take()` calls via `MarketLossFactorMaxedOut`, even though the market still has outstanding debt and credit.

### Finding Description

**Code path** — `src/Midnight.sol` lines 626–633:

```solidity
if (badDebt > 0) {
    _position.debt -= uint128(badDebt);
    uint256 _totalUnits = _marketState.totalUnits;
    uint256 _lossFactor = _marketState.lossFactor;
    _marketState.lossFactor = UtilsLib.toUint128(
        type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
    );
```

`mulDivDown` is defined in `src/libraries/UtilsLib.sol` line 29–31 as plain integer division:

```solidity
function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y) / d;
}
```

**Root cause** — When `_lossFactor = type(uint128).max - 1`:

- `type(uint128).max - _lossFactor = 1`
- The call becomes `mulDivDown(1, _totalUnits - badDebt, _totalUnits)`
- Integer division: `(1 * (_totalUnits - badDebt)) / _totalUnits`
- Since `_totalUnits - badDebt < _totalUnits` for any `badDebt >= 1`, this always evaluates to `0`
- Therefore `newLossFactor = type(uint128).max - 0 = type(uint128).max`

This holds for **any** `badDebt >= 1`, including `badDebt = 1` with `totalUnits = 1_000_000`, where only 0.0001% of the market's debt is bad.

**Reachability of `_lossFactor = type(uint128).max - 1`** — The state is reachable through normal market operation. Concrete example: `oldLossFactor = type(uint128).max - 2`, `badDebt = 1`, `totalUnits = 3`:
- `mulDivDown(2, 2, 3) = 4/3 = 1`
- `newLossFactor = type(uint128).max - 1` ✓

**Exploit flow:**
1. Market accumulates bad debt over its lifetime, driving `_lossFactor` to `type(uint128).max - 1` (reachable via the monotonically increasing update formula)
2. An unprivileged liquidator calls `liquidate(market, 0, 0, 0, borrower, false, receiver, address(0), "")` on any borrower with `badDebt >= 1`
3. The formula evaluates `mulDivDown(1, totalUnits - 1, totalUnits) = 0`
4. `_marketState.lossFactor` is set to `type(uint128).max`
5. All subsequent calls to `take()` revert with `MarketLossFactorMaxedOut` (line 349)

**Why existing checks fail** — There is no guard preventing `lossFactor` from reaching `type(uint128).max` when `badDebt < totalUnits`. The `liquidate()` function only requires `badDebt > 0` to enter the update block. The Certora formal verification rule `lossFactorChangesIffBadDebt` (LossFactor.spec line 53) explicitly preconditions on `lossFactorBefore < max_uint128`, meaning it does not cover the near-max regime.

### Impact Explanation
Once `lossFactor = type(uint128).max`, `take()` permanently reverts with `MarketLossFactorMaxedOut` for the affected market. Any remaining lenders with non-zero credit (even if small) cannot take new positions or reduce their exposure through the normal take mechanism. The market is frozen by a single liquidation call that realizes as little as 1 unit of bad debt, even when the market still has `totalUnits - 1` units of outstanding debt. This matches the scoped impact: bad debt misallocation (lenders lose more than the actual bad debt fraction) and position desynchronization (market blocked while positions remain open).

### Likelihood Explanation
**Preconditions:** `_lossFactor` must reach `type(uint128).max - 1`. This requires the market to have experienced many bad debt events, which is a realistic scenario for distressed markets over their lifetime. The state is reachable through normal liquidation activity without any privileged action.

**Feasibility:** Once the precondition is met, any unprivileged liquidator can trigger the saturation with a single zero-cost call (`seizedAssets = 0`, `repaidUnits = 0`). No tokens need to be transferred. The call is permissionless as long as the borrower is liquidatable (unhealthy or post-maturity).

**Repeatability:** The trigger is a single transaction. The state change is permanent and irreversible.

### Recommendation
Add a guard in the lossFactor update to cap the result at `type(uint128).max` only when `badDebt == totalUnits` (full bad debt). For partial bad debt, use `mulDivUp` on the subtracted term to ensure the complement never rounds to zero unless the debt is fully wiped:

```solidity
uint256 complement = (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits);
// Only allow saturation when all units are wiped
if (complement == 0 && badDebt < _totalUnits) complement = 1;
_marketState.lossFactor = UtilsLib.toUint128(type(uint128).max - complement);
```

Alternatively, use `mulDivUp` for the complement so that the subtracted term is never rounded below its true value, preventing premature saturation:

```solidity
_marketState.lossFactor = UtilsLib.toUint128(
    type(uint128).max - (type(uint128).max - _lossFactor).mulDivUp(_totalUnits - badDebt, _totalUnits)
);
```

Note: switching to `mulDivUp` changes the rounding direction for the complement (lenders lose slightly less per event), which is consistent with the protocol's stated intent of rounding "a bit more" against lenders only when there is genuine imprecision, not premature saturation.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {UtilsLib} from "src/libraries/UtilsLib.sol";

contract LossFactorPrematureSaturationTest is Test {
    using UtilsLib for uint256;

    /// @notice Unit test: demonstrates premature saturation when _lossFactor = max-1
    function testPrematureSaturation() public pure {
        uint256 MAX = type(uint128).max;
        uint256 _lossFactor = MAX - 1;
        uint256 totalUnits = 1_000_000;
        uint256 badDebt = 1; // only 1 unit of bad debt, far less than totalUnits

        uint256 complement = (MAX - _lossFactor).mulDivDown(totalUnits - badDebt, totalUnits);
        // complement = mulDivDown(1, 999_999, 1_000_000) = 999_999 / 1_000_000 = 0
        assertEq(complement, 0, "complement rounds to zero");

        uint256 newLossFactor = MAX - complement;
        assertEq(newLossFactor, MAX, "lossFactor prematurely saturated");

        // Assert the invariant that SHOULD hold: lossFactor < max iff badDebt < totalUnits
        // This assertion FAILS, proving the bug:
        assertTrue(newLossFactor < MAX, "FAILS: market saturated even though badDebt < totalUnits");
    }

    /// @notice Fuzz test: for any totalUnits > 1, badDebt=1, _lossFactor=max-1 always saturates
    function testFuzzPrematureSaturation(uint128 totalUnits) public pure {
        vm.assume(totalUnits > 1);
        uint256 MAX = type(uint128).max;
        uint256 _lossFactor = MAX - 1;
        uint256 badDebt = 1;

        uint256 complement = (MAX - _lossFactor).mulDivDown(uint256(totalUnits) - badDebt, totalUnits);
        uint256 newLossFactor = MAX - complement;

        // Invariant: newLossFactor < MAX iff badDebt < totalUnits
        // badDebt=1 < totalUnits (by assumption), so newLossFactor MUST be < MAX
        // This assertion FAILS for all totalUnits > 1:
        assertLt(newLossFactor, MAX, "premature saturation: lossFactor=max but badDebt < totalUnits");
    }
}
```

**Expected assertions:** Both `assertEq(complement, 0)` and `assertEq(newLossFactor, MAX)` pass, while `assertLt(newLossFactor, MAX)` fails — confirming the premature saturation. The fuzz test will fail for all `totalUnits > 1` with `badDebt = 1` and `_lossFactor = type(uint128).max - 1`.

---

**Supporting code references:**

The lossFactor update formula: [1](#0-0) 

The `mulDivDown` implementation (plain integer division, no overflow protection): [2](#0-1) 

The `take()` guard that permanently blocks the market: [3](#0-2) 

The protocol's own acknowledgment of near-max imprecision (but framed as "a bit more", not total premature saturation): [4](#0-3) 

The Certora rule that explicitly excludes the near-max regime from its proof: [5](#0-4)

### Citations

**File:** src/Midnight.sol (L117-120)
```text
/// @dev lossFactor is rounded up so lenders collectively lose a bit more than badDebt on each bad debt realization.
/// @dev If a market loses almost all of its value to bad debt over its lifetime, then the accounting of the loss
/// may become extremely imprecise (against the user), potentially leading to a total loss. Note that the take function
/// reverts when the loss factor is maxed out.
```

**File:** src/Midnight.sol (L349-349)
```text
        require(_marketState.lossFactor < type(uint128).max, MarketLossFactorMaxedOut());
```

**File:** src/Midnight.sol (L631-633)
```text
            _marketState.lossFactor = UtilsLib.toUint128(
                type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
            );
```

**File:** src/libraries/UtilsLib.sol (L28-31)
```text
    /// @dev Returns (x * y) / d rounded down.
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** certora/specs/LossFactor.spec (L48-61)
```text
rule lossFactorChangesIffBadDebt(env e, Midnight.Market market, uint256 collateralIndex, uint256 seizedAssets, uint256 repaidUnits, address borrower, address receiver, address callback, bytes data, bool postMaturityMode) {
    bytes32 id = summaryToId(market);
    uint128 lossFactorBefore = currentContract.marketState[id].lossFactor;
    uint256 totalUnitsBefore = totalUnits(id);

    require lossFactorBefore < max_uint128, "market lossFactor must not be saturated";

    liquidate(e, market, collateralIndex, seizedAssets, repaidUnits, borrower, postMaturityMode, receiver, callback, data);

    bool lossFactorChanged = currentContract.marketState[id].lossFactor != lossFactorBefore;
    bool badDebtOccurred = totalUnits(id) < totalUnitsBefore;

    assert lossFactorChanged <=> badDebtOccurred;
}
```
