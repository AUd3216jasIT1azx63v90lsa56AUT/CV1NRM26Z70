### Title
`mulDivDown` naive multiplication overflow in `supplyCollateralAndSellWithAssetsTarget` referral fee computation - (`File: src/periphery/MidnightBundles.sol`)

### Summary

`UtilsLib.mulDivDown` is implemented as plain `(x * y) / d` with no 512-bit intermediate handling. In `supplyCollateralAndSellWithAssetsTarget`, the referral fee is computed as `targetSellerAssets.mulDivDown(referralFeePct, WAD - referralFeePct)`. When `referralFeePct` is close to `WAD`, the denominator `WAD - referralFeePct` approaches 1, making the intermediate product `targetSellerAssets * referralFeePct` overflow `uint256` for large-but-realistic asset amounts, causing an unconditional arithmetic revert under Solidity 0.8.x checked arithmetic.

### Finding Description

**`UtilsLib.mulDivDown` implementation** (`src/libraries/UtilsLib.sol`, line 29–31):
```solidity
function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y) / d;
}
```
This is a naive implementation. There is no assembly `mulmod`-based 512-bit intermediate; the multiplication `x * y` is a plain checked Solidity multiplication that reverts on overflow.

**Vulnerable line** (`src/periphery/MidnightBundles.sol`, line 277):
```solidity
uint256 referralFeeAssets = targetSellerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
```

**Guard** (line 263):
```solidity
require(referralFeePct < WAD, PctExceeded());
```
This permits `referralFeePct` up to `WAD - 1` (i.e., `1e18 - 1`). With that value, `WAD - referralFeePct = 1`, and the call becomes `mulDivDown(targetSellerAssets, 1e18 - 1, 1)`, which internally computes `targetSellerAssets * (1e18 - 1)`. This overflows `uint256` whenever:

```
targetSellerAssets > type(uint256).max / (WAD - 1)
                   ≈ 1.157e77 / 1e18
                   ≈ 1.157e59
```

Tokens with 18 decimals and a total supply near `type(uint128).max` (~3.4e38) are well below this threshold, but tokens with 6 decimals (e.g., USDC) or any token whose raw amount is scaled up can reach this range. More importantly, `targetSellerAssets` is a **caller-supplied parameter with no upper-bound check**, so any caller can supply a value that triggers the overflow.

**Exploit flow:**
1. Attacker (borrower/taker) calls `supplyCollateralAndSellWithAssetsTarget` with `referralFeePct = WAD - 1` and `targetSellerAssets > type(uint256).max / (WAD - 1)`.
2. Line 263 passes (`WAD - 1 < WAD`).
3. Line 277 executes `mulDivDown(targetSellerAssets, WAD-1, 1)` → `(targetSellerAssets * (WAD-1)) / 1` → arithmetic overflow → unconditional revert.
4. No state changes have occurred yet (collateral supply loop at lines 269–275 may have executed, but the revert undoes them).

The existing fuzz test (`test/MidnightBundlesTest.sol`, line 633) deliberately bounds `referralFeePct` to `WAD / 2` and `targetSellerAssets` to `type(uint128).max / 4`, confirming the developers are aware of the overflow risk but the contract itself does not enforce these bounds.

### Impact Explanation

Any caller who supplies `referralFeePct` near `WAD` and a large `targetSellerAssets` receives an unexpected arithmetic revert instead of a clean `PctExceeded` or `SellerAssetsTooLow` error. This breaks the expected behavior of the sell-with-assets-target bundle: the function is supposed to either succeed or revert with a meaningful protocol error, not with a raw arithmetic panic. Collateral already pulled in the loop (lines 269–275) is rolled back by the revert, so no funds are lost, but the call is permanently unusable for that parameter combination.

### Likelihood Explanation

The precondition requires `referralFeePct` to be set near `WAD` (which the contract explicitly allows) and `targetSellerAssets` to exceed `~1.157e59`. For standard 18-decimal tokens this threshold is astronomically high and practically unreachable. For tokens with fewer decimals or in contexts where amounts are scaled, it is more reachable. The primary risk is a misconfigured or adversarial caller triggering an opaque panic revert rather than a protocol-defined error, which is a correctness/robustness issue. It is not exploitable to steal funds or corrupt state.

### Recommendation

Either:
1. **Cap `referralFeePct`** to a safe maximum (e.g., `WAD / 2`) in the `PctExceeded` guard, consistent with the fuzz test bounds:
   ```solidity
   require(referralFeePct <= WAD / 2, PctExceeded());
   ```
2. **Or replace `mulDivDown`** with a 512-bit-safe implementation (using `mulmod` in assembly, as in Solmate/Solady `FullMath`) so that the intermediate product never overflows regardless of inputs.

Option 1 is simpler and consistent with the existing test coverage. Option 2 is more general but changes the shared `UtilsLib` used throughout the protocol.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity 0.8.34;

import {Test, stdError} from "forge-std/Test.sol";
import {UtilsLib} from "src/libraries/UtilsLib.sol";
import {WAD} from "src/libraries/ConstantsLib.sol";

contract ReferralFeeOverflowTest is Test {
    using UtilsLib for uint256;

    // Reproduce the exact line 277 computation.
    function testReferralFeeOverflow() public {
        uint256 referralFeePct = WAD - 1;
        // Any value above type(uint256).max / (WAD - 1) triggers overflow.
        uint256 targetSellerAssets = type(uint256).max / (WAD - 1) + 1;

        // Expect arithmetic panic (overflow) — NOT PctExceeded.
        vm.expectRevert(stdError.arithmeticError);
        targetSellerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
    }

    // Fuzz: assert overflow boundary is exactly where expected.
    function testFuzzReferralFeeOverflowBoundary(uint256 targetSellerAssets) public {
        uint256 referralFeePct = WAD - 1;
        uint256 threshold = type(uint256).max / referralFeePct;

        if (targetSellerAssets <= threshold) {
            // Should not overflow.
            uint256 fee = targetSellerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
            assertEq(fee, (targetSellerAssets * referralFeePct) / 1);
        } else {
            // Must overflow.
            vm.expectRevert(stdError.arithmeticError);
            targetSellerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
        }
    }
}
```

**Expected assertions:**
- `testReferralFeeOverflow` passes: the call reverts with `arithmeticError` (panic 0x11), not `PctExceeded`.
- `testFuzzReferralFeeOverflowBoundary` passes for all inputs: overflow occurs exactly above the threshold. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** src/periphery/MidnightBundles.sol (L263-263)
```text
        require(referralFeePct < WAD, PctExceeded());
```

**File:** src/periphery/MidnightBundles.sol (L277-278)
```text
        uint256 referralFeeAssets = targetSellerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
        uint256 targetFilledSellerAssets = targetSellerAssets + referralFeeAssets;
```

**File:** test/MidnightBundlesTest.sol (L632-633)
```text
        targetSellerAssets = bound(targetSellerAssets, 1, uint256(type(uint128).max) / 4);
        referralFeePct = bound(referralFeePct, 0, WAD / 2);
```
