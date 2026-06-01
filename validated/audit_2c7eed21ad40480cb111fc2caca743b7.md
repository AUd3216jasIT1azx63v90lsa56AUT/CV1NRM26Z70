Audit Report

## Title
Missing budget invariant check causes revert when markup-formula referral fee exceeds remaining balance - (`src/periphery/MidnightBundles.sol`)

## Summary
`buyWithUnitsTargetAndWithdrawCollateral` uses a markup formula (`filledBuyerAssets * referralFeePct / (WAD - referralFeePct)`) to compute the referral fee after the fill loop completes. Because the fill loop is bounded only by `targetUnits` and not by the referral-fee-adjusted budget, `filledBuyerAssets + referralFeeAssets` can exceed `maxBuyerAssets`, causing the `safeTransfer` at line 103 to revert with an insufficient-balance error (or the subtraction at line 104 to panic). No guard exists between the fill loop and the transfer block to catch this condition, violating the NatSpec invariant "The msg.sender will pay at most `maxBuyerAssets`."

## Finding Description

**Exact code path** — `src/periphery/MidnightBundles.sol`: [1](#0-0) 

The NatSpec explicitly documents that the total cost is `filledBuyerAssets + filledBuyerAssets * referralFeePct / (WAD - referralFeePct)` and that this total must not exceed `maxBuyerAssets`. [2](#0-1) 

`maxBuyerAssets` is pulled upfront. [3](#0-2) 

The fill loop accumulates `filledBuyerAssets` and terminates only when `filledUnits == targetUnits`. There is no cap on `filledBuyerAssets` relative to `maxBuyerAssets * (WAD - referralFeePct) / WAD`. [4](#0-3) 

The only post-fill check is `require(filledUnits == targetUnits)` — no budget check. [5](#0-4) 

`referralFeeAssets` is computed using the markup formula. If `filledBuyerAssets > maxBuyerAssets * (WAD - referralFeePct) / WAD`, then `referralFeeAssets > maxBuyerAssets - filledBuyerAssets`. The contract's remaining balance after the fill is only `maxBuyerAssets - filledBuyerAssets`, so line 103's `safeTransfer` reverts with insufficient balance, or line 104's subtraction panics with arithmetic underflow.

**Root cause**: No invariant check `filledBuyerAssets + referralFeeAssets <= maxBuyerAssets` exists before the transfer block. The only guard is `require(referralFeePct < WAD)`, which does not bound the markup fee relative to the remaining budget.

**Contrast with `buyWithAssetsTargetAndWithdrawCollateral`**: That function correctly pre-computes `referralFeeAssets = targetBuyerAssets * referralFeePct / WAD` and sets `targetFilledBuyerAssets = targetBuyerAssets - referralFeeAssets` before the fill loop, so the fill is bounded by the fee-adjusted budget. [6](#0-5) 

`buyWithUnitsTargetAndWithdrawCollateral` has no equivalent pre-computation.

## Impact Explanation

The entire transaction reverts (either with an ERC-20 transfer failure at line 103 or an arithmetic panic at line 104). All state changes are rolled back atomically: the user acquires no credit, no collateral is withdrawn, and `maxBuyerAssets` tokens are returned. The user bears only the gas cost. The documented invariant "The msg.sender will pay at most `maxBuyerAssets`" is violated in the sense that the function fails to complete rather than succeeding with a refund, breaking the slippage-protection guarantee. Severity is low-to-medium: no funds are permanently lost, but the function is rendered unusable for any caller who sets `maxBuyerAssets` as their total budget and uses a non-zero referral fee with a tight fill.

## Likelihood Explanation

**Preconditions**:
1. `referralFeePct > 0` — any non-zero referral fee, set by the caller or a frontend.
2. `filledBuyerAssets > maxBuyerAssets * (WAD - referralFeePct) / WAD` — triggered whenever the fill consumes more than `(1 - referralFeePct/WAD)` of the budget.

For `referralFeePct = 10%`, the revert fires whenever fills consume more than 90% of `maxBuyerAssets`. A user who sets `maxBuyerAssets` as their total budget (the natural interpretation given the NatSpec) and uses any non-trivial referral fee will hit this on nearly every tight fill. No privileged role, oracle manipulation, or external state is required. The condition is deterministic and repeatable.

## Recommendation

Add an explicit budget check after computing `referralFeeAssets` and before the transfer block:

```solidity
uint256 referralFeeAssets = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
require(filledBuyerAssets + referralFeeAssets <= maxBuyerAssets, BudgetExceeded());
if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
SafeTransferLib.safeTransfer(loanToken, msg.sender, maxBuyerAssets - filledBuyerAssets - referralFeeAssets);
```

Alternatively, mirror the approach used in `buyWithAssetsTargetAndWithdrawCollateral`: pre-compute the maximum allowable `filledBuyerAssets` as `maxBuyerAssets * (WAD - referralFeePct) / WAD` before the fill loop and use it as an additional loop termination condition.

## Proof of Concept

Minimal Foundry test (extend the existing `MidnightBundlesTest` harness):

```solidity
function testBuyUnitsTargetReferralFeeUnderflow() public {
    // Setup: 10% referral fee, budget = 100e18
    uint256 referralFeePct = 0.1e18; // 10%
    uint256 maxBuyerAssets = 100e18;

    offers[0].buy = false;
    offers[0].maker = borrower;
    offers[0].receiverIfMakerIsSeller = borrower;
    offers[0].maxUnits = type(uint256).max;

    // Zero settlement fees for simplicity
    for (uint256 i; i <= 6; i++) midnight.setMarketSettlementFee(id, i, 0);

    // Choose units such that filledBuyerAssets ≈ 95e18 > 100e18 * 0.9 = 90e18
    uint256 price = TickLib.tickToPrice(MAX_TICK);
    uint256 units = uint256(95e18).mulDivUp(WAD, price);
    collateralize(market, borrower, units);

    Take[] memory takes = new Take[](1);
    takes[0] = Take({offer: offers[0], units: type(uint256).max, ratifierData: hex""});

    // Expect revert: referralFeeAssets ≈ 10.56e18 > remaining balance 5e18
    vm.prank(lender);
    vm.expectRevert(); // ERC-20 insufficient balance or arithmetic underflow
    midnightBundles.buyWithUnitsTargetAndWithdrawCollateral(
        units, maxBuyerAssets, lender, _noPermit(),
        takes, new CollateralWithdrawal[](0), address(0),
        referralFeePct, makeAddr("referrer")
    );
}
```

The test demonstrates that a normal unprivileged user calling the function with a 10% referral fee and a tight `maxBuyerAssets` budget causes a revert, confirming the missing invariant.

### Citations

**File:** src/periphery/MidnightBundles.sol (L45-48)
```text
    /// @dev This function pulls maxBuyerAssets from the msg.sender and transfers back the remaining tokens at the end.
    /// @dev The msg.sender will pay at most maxBuyerAssets.
    /// @dev Total loan assets transferred from msg.sender is
    /// filledBuyerAssets + filledBuyerAssets * referralFeePct / (WAD - referralFeePct).
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

**File:** src/periphery/MidnightBundles.sol (L88-88)
```text
        require(filledUnits == targetUnits, OutOfOffers());
```

**File:** src/periphery/MidnightBundles.sol (L102-104)
```text
        uint256 referralFeeAssets = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
        if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
        SafeTransferLib.safeTransfer(loanToken, msg.sender, maxBuyerAssets - filledBuyerAssets - referralFeeAssets);
```

**File:** src/periphery/MidnightBundles.sol (L200-201)
```text
        uint256 referralFeeAssets = targetBuyerAssets.mulDivDown(referralFeePct, WAD);
        uint256 targetFilledBuyerAssets = targetBuyerAssets - referralFeeAssets;
```
