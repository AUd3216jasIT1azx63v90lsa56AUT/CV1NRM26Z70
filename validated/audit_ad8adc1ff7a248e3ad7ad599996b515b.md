Audit Report

## Title
Fill loop lacks referral-fee budget ceiling causing unconditional revert in `buyWithUnitsTargetAndWithdrawCollateral` - (File: src/periphery/MidnightBundles.sol)

## Summary
`buyWithUnitsTargetAndWithdrawCollateral` pulls `maxBuyerAssets` from the caller and runs a fill loop bounded only by `filledUnits < targetUnits`, with no headroom reserved for the referral fee. Post-loop, the referral fee `filledBuyerAssets * referralFeePct / (WAD - referralFeePct)` can exceed the bundler's remaining balance `maxBuyerAssets - filledBuyerAssets`, causing an unconditional revert on the `safeTransfer` to `referralFeeRecipient` or an arithmetic underflow on the refund transfer. Any call with `referralFeePct > 0` and a tight `maxBuyerAssets` reliably triggers this.

## Finding Description

**Exact code path:**

The bundler pulls `maxBuyerAssets` from the caller: [1](#0-0) 

The fill loop accumulates `filledBuyerAssets` with no ceiling tied to the referral fee: [2](#0-1) 

After the loop, the bundler holds `maxBuyerAssets - filledBuyerAssets`. The referral fee is then computed and transferred: [3](#0-2) 

For line 103 to succeed, the bundler must hold at least `referralFeeAssets`. This requires:

```
filledBuyerAssets * referralFeePct / (WAD - referralFeePct) <= maxBuyerAssets - filledBuyerAssets
```

Solving: `filledBuyerAssets <= maxBuyerAssets * (WAD - referralFeePct) / WAD`

The fill loop enforces no such constraint. When `filledBuyerAssets` exceeds this threshold, line 103 reverts due to insufficient balance, or line 104 underflows under Solidity 0.8 checked arithmetic.

**Root cause:** The loop termination condition is `filledUnits < targetUnits` only. There is no pre-deduction of the referral fee from the available fill budget before the loop begins.

**Contrast with sibling function:** `buyWithAssetsTargetAndWithdrawCollateral` correctly pre-deducts the referral fee before the fill loop: [4](#0-3) 

The loop is bounded by `filledBuyerAssets < targetFilledBuyerAssets`, ensuring the referral fee is always coverable. `buyWithUnitsTargetAndWithdrawCollateral` has no equivalent.

**NatSpec contradiction:** Line 46 states "The msg.sender will pay at most `maxBuyerAssets`" — this invariant is broken because the function reverts rather than capping the fill: [5](#0-4) 

**Existing checks are insufficient:**
- `require(referralFeePct < WAD)` (line 61) only prevents a 100% fee; any value in `[1, WAD-1]` is accepted. [6](#0-5) 
- `require(filledUnits == targetUnits)` (line 88) checks unit completeness only, not budget adequacy. [7](#0-6) 
- There is no `require(filledBuyerAssets + referralFeeAssets <= maxBuyerAssets)` anywhere.

## Impact Explanation
Any call to `buyWithUnitsTargetAndWithdrawCollateral` with `referralFeePct > 0` where `filledBuyerAssets > maxBuyerAssets * (WAD - referralFeePct) / WAD` reverts unconditionally. The caller loses gas, no fill is executed, and no funds are permanently lost (revert). The function is effectively unusable with referral fees unless the caller sets `maxBuyerAssets` to `expectedFill * WAD / (WAD - referralFeePct)` — a non-obvious requirement that is neither enforced nor documented. This is a concrete DoS of a core bundler function for any caller using referral fees with a tight `maxBuyerAssets`.

## Likelihood Explanation
Preconditions are minimal: `referralFeePct > 0` (any non-zero referral fee) and `filledBuyerAssets` large relative to `maxBuyerAssets`. A caller who sets `maxBuyerAssets` to the expected fill cost — the natural interpretation of the parameter name and the "pay at most" NatSpec — will reliably trigger this whenever `referralFeePct > 0`. No privileged access, special market state, or victim mistake is required. The condition is deterministic given fixed offer prices and settlement fees, and repeatable across blocks.

## Recommendation
Pre-deduct the referral fee from the fill budget before the loop, mirroring the pattern in `buyWithAssetsTargetAndWithdrawCollateral`:

```solidity
uint256 referralFeeAssets = maxBuyerAssets.mulDivDown(referralFeePct, WAD);
uint256 maxFilledBuyerAssets = maxBuyerAssets - referralFeeAssets;
```

Then cap `filledBuyerAssets` within the loop to `maxFilledBuyerAssets`, and after the loop transfer `referralFeeAssets` (pre-computed) to `referralFeeRecipient` and refund `maxFilledBuyerAssets - filledBuyerAssets` to `msg.sender`. Alternatively, add a post-loop guard: `require(filledBuyerAssets + referralFeeAssets <= maxBuyerAssets)`.

## Proof of Concept
**Minimal manual steps:**
1. Deploy `MidnightBundles` against a live or forked Midnight instance.
2. Create a market with a sell offer priced at 1 loanToken per unit.
3. Call `buyWithUnitsTargetAndWithdrawCollateral` with:
   - `targetUnits = 100`
   - `maxBuyerAssets = 100e18` (exact expected fill cost)
   - `referralFeePct = 0.01e18` (1%, i.e., `WAD / 100`)
   - `referralFeeRecipient = any address`
4. The fill loop fills `filledBuyerAssets = 100e18`. The bundler's remaining balance is `0`. `referralFeeAssets = 100e18 * 0.01e18 / (1e18 - 0.01e18) ≈ 1.01e18 > 0`. Line 103 reverts with insufficient balance.
5. Confirm: setting `maxBuyerAssets = 101.01e18` (i.e., `expectedFill * WAD / (WAD - referralFeePct)`) allows the call to succeed, confirming the root cause.

### Citations

**File:** src/periphery/MidnightBundles.sol (L46-48)
```text
    /// @dev The msg.sender will pay at most maxBuyerAssets.
    /// @dev Total loan assets transferred from msg.sender is
    /// filledBuyerAssets + filledBuyerAssets * referralFeePct / (WAD - referralFeePct).
```

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
