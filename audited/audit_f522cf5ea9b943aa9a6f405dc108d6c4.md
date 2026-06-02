Audit Report

## Title
Arithmetic underflow in refund calculation causes opaque panic revert when `referralFeeAssets` is not covered by `maxBuyerAssets` - (File: src/periphery/MidnightBundles.sol)

## Summary

`buyWithUnitsTargetAndWithdrawCollateral` fills orders accumulating `filledBuyerAssets`, then computes `referralFeeAssets` on top of that amount and attempts to refund `maxBuyerAssets - filledBuyerAssets - referralFeeAssets` with no prior guard. When `maxBuyerAssets` covers the fill cost but not the additional referral fee, the subtraction underflows in Solidity 0.8.34, reverting with an arithmetic panic. No funds are lost (the revert rolls back `pullToken`), but the function fails opaquely instead of reverting with a meaningful error, violating the NatSpec invariant "The msg.sender will pay at most maxBuyerAssets."

## Finding Description

**Exact code path:**

- Line 66: `pullToken(loanToken, msg.sender, maxBuyerAssets, ...)` — bundler holds `maxBuyerAssets`. [1](#0-0) 

- Lines 71–86: the fills loop accumulates `filledBuyerAssets` with no upper bound derived from a fee-adjusted budget. [2](#0-1) 

- Line 102: `referralFeeAssets = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct)` — fee is computed *after* fills complete. [3](#0-2) 

- Line 104: `SafeTransferLib.safeTransfer(loanToken, msg.sender, maxBuyerAssets - filledBuyerAssets - referralFeeAssets)` — unchecked subtraction; underflows when `filledBuyerAssets + referralFeeAssets > maxBuyerAssets`. [4](#0-3) 

**Root cause:** The function design requires `maxBuyerAssets >= filledBuyerAssets * WAD / (WAD - referralFeePct)`, but this is never validated. There is no `require(filledBuyerAssets + referralFeeAssets <= maxBuyerAssets, ...)` before line 104, and the fills loop has no fee-adjusted budget cap.

**Why existing checks fail:** Line 61 only checks `referralFeePct < WAD`. [5](#0-4) 
The fills loop termination condition is `filledUnits < targetUnits`, not a fee-adjusted asset cap. [6](#0-5) 

**Test coverage gap:** The existing fuzz test `testBuyUnitsTargetWithReferralFee` always passes `type(uint256).max` as `maxBuyerAssets`, which never triggers the underflow path. [7](#0-6) 

**Concrete exploit flow:**
```
referralFeePct   = 0.01e18  (1%)
filledBuyerAssets = 100e18
referralFeeAssets = mulDivDown(100e18, 0.01e18, 0.99e18) ≈ 1.0101e18
maxBuyerAssets   = 100e18   (user set to expected fill cost)

Line 104: 100e18 - 100e18 - 1.0101e18  →  arithmetic underflow → panic revert
```

## Impact Explanation

Every call to `buyWithUnitsTargetAndWithdrawCollateral` with `referralFeePct > 0` and `maxBuyerAssets` set to the expected fill cost (without the fee premium) reverts with an arithmetic underflow panic. The user's transaction fails as a no-op; no funds are permanently lost because the revert rolls back `pullToken`. The concrete impact is a broken slippage-protection invariant: instead of a clean, descriptive revert (e.g., `SlippageExceeded`), the caller receives an opaque panic, making the failure undiagnosable without source-level debugging.

## Likelihood Explanation

Any unprivileged caller who passes a nonzero `referralFeePct` and sets `maxBuyerAssets` to their expected fill cost — a natural interpretation of the parameter name and the "at most" NatSpec guarantee — will hit this. The condition `filledBuyerAssets + referralFeeAssets > maxBuyerAssets` is trivially satisfied whenever `maxBuyerAssets == filledBuyerAssets` and `referralFeePct > 0`. The trigger is deterministic and repeatable for any such input combination.

## Recommendation

Add an explicit check before the refund transfer:

```solidity
require(filledBuyerAssets + referralFeeAssets <= maxBuyerAssets, SlippageExceeded());
```

Alternatively, cap `filledBuyerAssets` inside the fills loop using a fee-adjusted budget: compute `maxFilledBuyerAssets = maxBuyerAssets.mulDivDown(WAD - referralFeePct, WAD)` before the loop and stop filling once `filledBuyerAssets >= maxFilledBuyerAssets`. This mirrors the approach used in `buyWithAssetsTargetAndWithdrawCollateral` (lines 200–201). [8](#0-7) 

## Proof of Concept

Minimal Foundry test (add to `MidnightBundlesTest.sol`):

```solidity
function testBuyUnitsTargetUnderflowWithReferralFee() public {
    uint256 units = 1e18;
    uint256 referralFeePct = 0.01e18; // 1%

    collateralize(market, borrower, units);

    Take[] memory takes = new Take[](1);
    takes[0] = Take({offer: offers[0], units: type(uint256).max, ratifierData: hex""});

    // Compute expected fill cost (without fee)
    uint256 price = TickLib.tickToPrice(MAX_TICK);
    uint256 expectedFill = units.mulDivUp(price, WAD);

    // maxBuyerAssets set to fill cost only, not fill + fee
    vm.prank(lender);
    vm.expectRevert(); // arithmetic panic 0x11
    midnightBundles.buyWithUnitsTargetAndWithdrawCollateral(
        units,
        expectedFill,   // <-- does NOT include referral fee premium
        lender,
        _noPermit(),
        takes,
        new CollateralWithdrawal[](0),
        address(0),
        referralFeePct,
        makeAddr("referrer")
    );
}
```

### Citations

**File:** src/periphery/MidnightBundles.sol (L61-61)
```text
        require(referralFeePct < WAD, PctExceeded());
```

**File:** src/periphery/MidnightBundles.sol (L66-66)
```text
        pullToken(loanToken, msg.sender, maxBuyerAssets, loanTokenPermit);
```

**File:** src/periphery/MidnightBundles.sol (L71-86)
```text
        for (uint256 i; i < takes.length && filledUnits < targetUnits; i++) {
            require(!takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
            uint256 unitsToTake = min(
                targetUnits - filledUnits,
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
            try IMidnight(MIDNIGHT)
                .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (
                uint256 resBuyerAssets, uint256
            ) {
                filledUnits += unitsToTake;
                filledBuyerAssets += resBuyerAssets;
            } catch {}
        }
```

**File:** src/periphery/MidnightBundles.sol (L102-102)
```text
        uint256 referralFeeAssets = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
```

**File:** src/periphery/MidnightBundles.sol (L104-104)
```text
        SafeTransferLib.safeTransfer(loanToken, msg.sender, maxBuyerAssets - filledBuyerAssets - referralFeeAssets);
```

**File:** src/periphery/MidnightBundles.sol (L200-201)
```text
        uint256 referralFeeAssets = targetBuyerAssets.mulDivDown(referralFeePct, WAD);
        uint256 targetFilledBuyerAssets = targetBuyerAssets - referralFeeAssets;
```

**File:** test/MidnightBundlesTest.sol (L534-544)
```text
        midnightBundles.buyWithUnitsTargetAndWithdrawCollateral(
            units,
            type(uint256).max,
            lender,
            _noPermit(),
            takes,
            new CollateralWithdrawal[](0),
            address(0),
            referralFeePct,
            referrer
        );
```
