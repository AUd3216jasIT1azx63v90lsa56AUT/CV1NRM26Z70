Audit Report

## Title
Fill loop lacks referral-fee budget ceiling causing unconditional revert in `buyWithUnitsTargetAndWithdrawCollateral` - (File: src/periphery/MidnightBundles.sol)

## Summary
`buyWithUnitsTargetAndWithdrawCollateral` pulls `maxBuyerAssets` from the caller, then runs a fill loop bounded only by `filledUnits < targetUnits` with no ceiling that reserves headroom for the referral fee. After the loop the bundler holds `maxBuyerAssets - filledBuyerAssets`. The referral fee is computed as `filledBuyerAssets * referralFeePct / (WAD - referralFeePct)` and can exceed that remainder, causing an unconditional revert at the `safeTransfer` to `referralFeeRecipient` (line 103) or an arithmetic underflow at line 104. Any call with `referralFeePct > 0` where `filledBuyerAssets > maxBuyerAssets * (WAD - referralFeePct) / WAD` fails, making the function a DoS for callers using referral fees with a tight `maxBuyerAssets`.

## Finding Description

**Exact code path:**

The fill loop (lines 71–86) accumulates `filledBuyerAssets` with no upper bound tied to the referral fee: [1](#0-0) 

After the loop, the bundler's token balance is `maxBuyerAssets - filledBuyerAssets`. The referral fee is then computed and transferred: [2](#0-1) 

For line 103 to succeed, the bundler must hold at least `referralFeeAssets`. This requires:

```
filledBuyerAssets * referralFeePct / (WAD - referralFeePct) <= maxBuyerAssets - filledBuyerAssets
```

Solving: `filledBuyerAssets <= maxBuyerAssets * (WAD - referralFeePct) / WAD`

The fill loop enforces no such constraint. When `filledBuyerAssets` exceeds this threshold, line 103 reverts due to insufficient balance, or line 104 underflows under Solidity 0.8 checked arithmetic.

**Root cause:** The loop termination condition is `filledUnits < targetUnits` only. There is no pre-deduction of the referral fee from the available fill budget before the loop begins.

**Contrast with sibling function:** `buyWithAssetsTargetAndWithdrawCollateral` correctly pre-deducts the referral fee before the fill loop: [3](#0-2) 

The loop is bounded by `filledBuyerAssets < targetFilledBuyerAssets`, ensuring the referral fee is always coverable. `buyWithUnitsTargetAndWithdrawCollateral` has no equivalent.

**NatSpec contradiction:** Line 46 states "The msg.sender will pay at most `maxBuyerAssets`" — this invariant is broken because the function reverts rather than capping the fill. [4](#0-3) 

**Existing checks are insufficient:**
- `require(referralFeePct < WAD)` (line 61) only prevents a 100% fee; any value in `[1, WAD-1]` is accepted. [5](#0-4) 
- `require(filledUnits == targetUnits)` (line 88) checks unit completeness only, not budget adequacy. [6](#0-5) 
- There is no `require(filledBuyerAssets + referralFeeAssets <= maxBuyerAssets)` anywhere.

## Impact Explanation
Any call to `buyWithUnitsTargetAndWithdrawCollateral` with `referralFeePct > 0` where `filledBuyerAssets > maxBuyerAssets * (WAD - referralFeePct) / WAD` reverts unconditionally. The caller loses gas, no fill is executed, and no funds are permanently lost (revert). The function is effectively unusable with referral fees unless the caller sets `maxBuyerAssets` to `expectedFill * WAD / (WAD - referralFeePct)` — a non-obvious requirement that is not enforced or clearly documented. This is a concrete DoS of a core bundler function for any caller using referral fees with a tight `maxBuyerAssets`.

## Likelihood Explanation
Preconditions are minimal: `referralFeePct > 0` (any non-zero referral fee) and `filledBuyerAssets` large relative to `maxBuyerAssets`. A caller who sets `maxBuyerAssets` to the expected fill cost — the natural interpretation of the parameter name and the "pay at most" NatSpec — will reliably trigger this whenever `referralFeePct > 0`. No privileged access, special market state, or victim mistake is required. The condition is deterministic given fixed offer prices and settlement fees, and repeatable across blocks.

## Recommendation
Pre-deduct the referral fee from the available fill budget before the loop, mirroring `buyWithAssetsTargetAndWithdrawCollateral`:

```solidity
uint256 referralFeeAssets = maxBuyerAssets.mulDivDown(referralFeePct, WAD);
uint256 maxFilledBuyerAssets = maxBuyerAssets - referralFeeAssets;
// ... fill loop bounded by filledBuyerAssets <= maxFilledBuyerAssets ...
// After loop, recompute actual referralFeeAssets from filledBuyerAssets
uint256 actualReferralFeeAssets = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
```

Alternatively, add a post-fill check: `require(filledBuyerAssets + referralFeeAssets <= maxBuyerAssets, BudgetExceeded())` before the transfers at lines 103–104.

## Proof of Concept
**Manual steps:**
1. Deploy `MidnightBundles` against a live or forked Midnight instance.
2. Create a market with a sell offer priced such that filling `targetUnits` costs `2e18` loan tokens.
3. Call `buyWithUnitsTargetAndWithdrawCollateral` with:
   - `maxBuyerAssets = 3e18`
   - `referralFeePct = 0.5e18` (50%)
   - `takes` array pointing at the offer above
   - `targetUnits` = units corresponding to `2e18` fill cost
4. Observe: `filledBuyerAssets = 2e18`, `referralFeeAssets = 2e18 * 0.5e18 / 0.5e18 = 2e18`, bundler balance = `1e18`. Line 103 attempts `safeTransfer(..., 2e18)` → **revert**.

**Fuzz test plan:** Fuzz `(maxBuyerAssets, referralFeePct, filledBuyerAssets)` with the invariant `filledBuyerAssets + filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct) <= maxBuyerAssets`. Any counterexample where `filledBuyerAssets > maxBuyerAssets * (WAD - referralFeePct) / WAD` and `referralFeePct > 0` reproduces the revert.

### Citations

**File:** src/periphery/MidnightBundles.sol (L45-48)
```text
    /// @dev This function pulls maxBuyerAssets from the msg.sender and transfers back the remaining tokens at the end.
    /// @dev The msg.sender will pay at most maxBuyerAssets.
    /// @dev Total loan assets transferred from msg.sender is
    /// filledBuyerAssets + filledBuyerAssets * referralFeePct / (WAD - referralFeePct).
```

**File:** src/periphery/MidnightBundles.sol (L61-61)
```text
        require(referralFeePct < WAD, PctExceeded());
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

**File:** src/periphery/MidnightBundles.sol (L200-205)
```text
        uint256 referralFeeAssets = targetBuyerAssets.mulDivDown(referralFeePct, WAD);
        uint256 targetFilledBuyerAssets = targetBuyerAssets - referralFeeAssets;

        uint256 filledUnits;
        uint256 filledBuyerAssets;
        for (uint256 i; i < takes.length && filledBuyerAssets < targetFilledBuyerAssets; i++) {
```
