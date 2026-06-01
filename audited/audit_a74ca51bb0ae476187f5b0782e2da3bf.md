### Title
lossFactor Precision Loss Destroys 100% of Lender Credit on 50% Bad Debt Event at Dust-Level totalUnits - (File: src/libraries/UtilsLib.sol / src/Midnight.sol)

### Summary
When `totalUnits` is very small (e.g., 2) and a bad debt event covers exactly half (badDebt=1), the `mulDivDown` rounding in the `lossFactor` update produces a `lossFactor` of `2^127`, and the subsequent per-lender slash formula then rounds each lender's `postSlashCredit` to zero. Two lenders each holding `credit=1` both have their entire credit destroyed despite only a 50% bad debt event, violating the invariant that the sum of lender credits equals remaining `totalUnits`.

### Finding Description

**Code path 1 — lossFactor update in `liquidate` (`src/Midnight.sol` lines 631–633):**

```solidity
_marketState.lossFactor = UtilsLib.toUint128(
    type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
);
```

With `_lossFactor = 0`, `_totalUnits = 2`, `badDebt = 1`:

```
newLossFactor = (2^128 - 1) - floor((2^128 - 1) * 1 / 2)
              = (2^128 - 1) - (2^127 - 1)   // 2^128-1 is odd, floor rounds down
              = 2^127
```

**Code path 2 — per-lender slash in `updatePositionView` (`src/Midnight.sol` lines 805–807):**

```solidity
uint256 postSlashCredit = _lastLossFactor < type(uint128).max
    ? credit.mulDivDown(type(uint128).max - marketState[id].lossFactor, type(uint128).max - _lastLossFactor)
    : 0;
```

With `credit = 1`, `_lastLossFactor = 0`, `marketState[id].lossFactor = 2^127`:

```
postSlashCredit = floor(1 * (2^128 - 1 - 2^127) / (2^128 - 1))
               = floor((2^127 - 1) / (2^128 - 1))
               = 0   // numerator < denominator
```

Both lenders receive `postSlashCredit = 0`. Sum of post-slash credits = 0, but `totalUnits` after bad debt = 1. The 1 remaining unit of debt has no corresponding credit.

**Attacker inputs / exploit flow:**

1. A market reaches `totalUnits = 2` (either by design with dust amounts, or naturally after accumulated bad debt events reduce a larger market).
2. Two lenders each hold `credit = 1`, `lastLossFactor = 0`.
3. A borrower has `debt = 2` with collateral whose oracle price drops to create exactly `badDebt = 1`.
4. Any unprivileged liquidator calls `liquidate(market, 0, 0, 0, borrower, false, receiver, address(0), "")`.
5. `lossFactor` is set to `2^127`; `totalUnits` drops to 1.
6. Both lenders call `updatePosition`; each receives `postSlashCredit = 0`.

**Why existing checks fail:**

There is no minimum `totalUnits` or minimum `credit` guard anywhere in `liquidate` or `updatePositionView`. The `mulDivDown` in `UtilsLib.sol` (line 30) is a plain integer division with no floor-correction. The protocol comment at lines 117–120 acknowledges "potentially leading to a total loss" only in the context of a market losing "almost all of its value … over its lifetime," not a single 50% event.

### Impact Explanation

Lenders holding `credit = 1` in a market with `totalUnits = 2` lose 100% of their credit on a 50% bad debt event. The correct proportional loss is 50% (0.5 units, rounding to 0 or 1). Instead, both lenders are rounded to 0, destroying 1 unit of credit that should survive. The invariant `sum(lender credits) == totalUnits` is broken: 0 ≠ 1. The surviving debt unit becomes unclaimable, permanently misallocating the loss.

### Likelihood Explanation

**Preconditions:**
- `totalUnits` must be very small (≤ a few units). This is reachable in two ways: (a) a market is bootstrapped with dust amounts (no minimum enforced), or (b) a legitimate market suffers repeated bad debt events that progressively reduce `totalUnits` to dust.
- A borrower must have debt equal to `totalUnits` with collateral that creates exactly 50% bad debt.
- Any unprivileged liquidator can trigger the final step.

**Feasibility:** Low for a fresh market (requires dust amounts that lenders would not willingly enter), but realistic for a market that has been degraded by prior bad debt events. The liquidator needs no special privilege — `liquidate` is permissionless for unhealthy/post-maturity positions.

**Repeatability:** Each bad debt event at dust-level `totalUnits` repeats the same destruction.

### Recommendation

In `updatePositionView`, apply a minimum-of-1 floor when `credit > 0` and `postSlashCredit` rounds to 0 but the market still has `totalUnits > 0`, OR enforce a protocol-level minimum `totalUnits` threshold below which the market is considered fully defaulted (set `lossFactor = type(uint128).max`). Concretely, in the `liquidate` bad-debt block, if `_totalUnits - badDebt == 0` OR if `(_totalUnits - badDebt)` is so small that the `mulDivDown` would saturate `lossFactor` to `type(uint128).max`, set `lossFactor = type(uint128).max` directly and zero all remaining credit explicitly, rather than leaving a phantom surviving `totalUnits` with no credit backing.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {UtilsLib} from "src/libraries/UtilsLib.sol";

contract LossFactorDustTest is Test {
    using UtilsLib for uint256;

    function testDustTotalUnitsDestroys100PercentCredit() public pure {
        uint256 totalUnits = 2;
        uint256 badDebt    = 1;
        uint256 lossFactor = 0;

        // Step 1: compute new lossFactor as in Midnight.sol:631-633
        uint256 newLossFactor =
            type(uint128).max -
            (type(uint128).max - lossFactor).mulDivDown(totalUnits - badDebt, totalUnits);

        // newLossFactor == 2^127
        assertEq(newLossFactor, uint256(1) << 127, "lossFactor should be 2^127");

        // Step 2: compute postSlashCredit for each lender (credit=1, lastLossFactor=0)
        // as in Midnight.sol:805-807
        uint256 credit = 1;
        uint256 postSlashCredit =
            credit.mulDivDown(type(uint128).max - newLossFactor, type(uint128).max - lossFactor);

        // postSlashCredit == 0 due to rounding
        assertEq(postSlashCredit, 0, "postSlashCredit rounds to 0");

        // Step 3: two lenders, sum of post-slash credits
        uint256 sumCredits = postSlashCredit + postSlashCredit; // 0 + 0 = 0

        // Invariant: sum of credits should equal remaining totalUnits (= 1)
        // This assertion FAILS, proving the bug
        assertEq(sumCredits, totalUnits - badDebt,
            "INVARIANT BROKEN: sum of post-slash credits != remaining totalUnits");
    }
}
```

**Expected result:** The final `assertEq` fails with `0 != 1`, confirming that both lenders lose 100% of their credit on a 50% bad debt event, and the surviving `totalUnits = 1` has no credit backing. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** src/Midnight.sol (L117-120)
```text
/// @dev lossFactor is rounded up so lenders collectively lose a bit more than badDebt on each bad debt realization.
/// @dev If a market loses almost all of its value to bad debt over its lifetime, then the accounting of the loss
/// may become extremely imprecise (against the user), potentially leading to a total loss. Note that the take function
/// reverts when the loss factor is maxed out.
```

**File:** src/Midnight.sol (L631-633)
```text
            _marketState.lossFactor = UtilsLib.toUint128(
                type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
            );
```

**File:** src/Midnight.sol (L805-807)
```text
        uint256 postSlashCredit = _lastLossFactor < type(uint128).max
            ? credit.mulDivDown(type(uint128).max - marketState[id].lossFactor, type(uint128).max - _lastLossFactor)
            : 0;
```

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```
