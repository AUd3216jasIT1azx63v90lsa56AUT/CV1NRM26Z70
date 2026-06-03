The code confirms the claim. Let me verify each assertion:

**Line 66** — bundler pulls `maxBuyerAssets` from caller: [1](#0-0) 

**Lines 71–86** — fill loop bounded only by `filledUnits < targetUnits`, no referral-fee headroom: [2](#0-1) 

**Lines 102–104** — referral fee computed post-loop and transferred; line 104 underflows if `referralFeeAssets > maxBuyerAssets - filledBuyerAssets`: [3](#0-2) 

**Lines 200–205** — sibling `buyWithAssetsTargetAndWithdrawCollateral` correctly pre-deducts the referral fee before the loop: [4](#0-3) 

**Lines 46–48** — NatSpec states "pay at most `maxBuyerAssets`" but the function reverts instead of capping: [5](#0-4) 

All claims check out against the actual code. The vulnerability is valid.

---

Audit Report

## Title
Fill loop lacks referral-fee budget ceiling causing unconditional revert in `buyWithUnitsTargetAndWithdrawCollateral` - (File: src/periphery/MidnightBundles.sol)

## Summary
`buyWithUnitsTargetAndWithdrawCollateral` pulls `maxBuyerAssets` from the caller and runs a fill loop bounded only by `filledUnits < targetUnits`, with no headroom reserved for the referral fee. Post-loop, the referral fee `filledBuyerAssets * referralFeePct / (WAD - referralFeePct)` can exceed the bundler's remaining balance `maxBuyerAssets - filledBuyerAssets`, causing an unconditional revert on the `safeTransfer` to `referralFeeRecipient` or an arithmetic underflow on the refund transfer. Any call with `referralFeePct > 0` and a tight `maxBuyerAssets` reliably triggers this.

## Finding Description

The bundler pulls `maxBuyerAssets` from the caller at line 66. During the fill loop (lines 71–86), MIDNIGHT pulls `filledBuyerAssets` from the bundler via the pre-granted approval. After the loop the bundler holds exactly `maxBuyerAssets - filledBuyerAssets`.

At line 102, the referral fee is computed as:
```
referralFeeAssets = filledBuyerAssets * referralFeePct / (WAD - referralFeePct)
```

For line 103 (`safeTransfer` to `referralFeeRecipient`) to succeed, the bundler must hold at least `referralFeeAssets`, requiring:
```
filledBuyerAssets * referralFeePct / (WAD - referralFeePct) <= maxBuyerAssets - filledBuyerAssets
```
Solving: `filledBuyerAssets <= maxBuyerAssets * (WAD - referralFeePct) / WAD`

The fill loop enforces no such constraint. When `filledBuyerAssets` exceeds this threshold, line 103 reverts due to insufficient token balance, or line 104 (`maxBuyerAssets - filledBuyerAssets - referralFeeAssets`) underflows under Solidity 0.8 checked arithmetic.

**Root cause:** The loop termination condition is `filledUnits < targetUnits` only. There is no pre-deduction of the referral fee from the available fill budget before the loop begins.

**Contrast with sibling function:** `buyWithAssetsTargetAndWithdrawCollateral` (lines 200–205) correctly pre-deducts the referral fee before the fill loop:
```solidity
uint256 referralFeeAssets = targetBuyerAssets.mulDivDown(referralFeePct, WAD);
uint256 targetFilledBuyerAssets = targetBuyerAssets - referralFeeAssets;
// loop bounded by filledBuyerAssets < targetFilledBuyerAssets
```
`buyWithUnitsTargetAndWithdrawCollateral` has no equivalent guard.

**NatSpec contradiction:** Line 46 states "The msg.sender will pay at most `maxBuyerAssets`" — this invariant is broken because the function reverts rather than capping the fill when the referral fee would exceed the remaining balance.

**Existing checks are insufficient:**
- `require(referralFeePct < WAD)` (line 61) only prevents a 100% fee; any value in `[1, WAD-1]` is accepted.
- `require(filledUnits == targetUnits)` (line 88) checks unit completeness only, not budget adequacy.
- There is no `require(filledBuyerAssets + referralFeeAssets <= maxBuyerAssets)` anywhere.

## Impact Explanation
Any call to `buyWithUnitsTargetAndWithdrawCollateral` with `referralFeePct > 0` where `filledBuyerAssets > maxBuyerAssets * (WAD - referralFeePct) / WAD` reverts unconditionally. The caller loses gas, no fill is executed, and no funds are permanently lost (revert). The function is effectively unusable with referral fees unless the caller sets `maxBuyerAssets` to `expectedFill * WAD / (WAD - referralFeePct)` — a non-obvious requirement that is neither enforced nor documented. This is a concrete DoS of a core bundler function for any caller using referral fees with a tight `maxBuyerAssets`.

## Likelihood Explanation
Preconditions are minimal: `referralFeePct > 0` (any non-zero referral fee) and `filledBuyerAssets` large relative to `maxBuyerAssets`. A caller who sets `maxBuyerAssets` to the expected fill cost — the natural interpretation of the parameter name and the "pay at most" NatSpec — will reliably trigger this whenever `referralFeePct > 0`. No privileged access, special market state, or victim mistake is required. The condition is deterministic given fixed offer prices and settlement fees, and repeatable across blocks.

## Recommendation
Mirror the pattern used in `buyWithAssetsTargetAndWithdrawCollateral`. Before the fill loop, pre-deduct the referral fee from the available fill budget:

```solidity
uint256 maxFilledBuyerAssets = maxBuyerAssets.mulDivDown(WAD - referralFeePct, WAD);
```

Then cap `filledBuyerAssets` to `maxFilledBuyerAssets` inside the loop (analogous to how `buyWithAssetsTargetAndWithdrawCollateral` caps by `targetFilledBuyerAssets`). After the loop, compute `referralFeeAssets` from `filledBuyerAssets` and transfer the refund as `maxBuyerAssets - filledBuyerAssets - referralFeeAssets`. This guarantees the referral fee is always coverable from the pulled funds and the refund never underflows.

## Proof of Concept
Minimal manual steps:
1. Deploy `MidnightBundles` against a live or forked Midnight instance.
2. Create a market with a sell offer priced such that filling `targetUnits` costs exactly `C` buyer assets.
3. Call `buyWithUnitsTargetAndWithdrawCollateral` with `maxBuyerAssets = C` and `referralFeePct = 0.01e18` (1%).
4. The fill loop fills `targetUnits`, spending `C` buyer assets (`filledBuyerAssets = C`).
5. Post-loop: bundler holds `0`. `referralFeeAssets = C * 0.01e18 / (1e18 - 0.01e18) > 0`.
6. Line 103 reverts: bundler has zero balance, cannot transfer `referralFeeAssets`.

Fuzz test plan: fuzz over `(referralFeePct, maxBuyerAssets, offerPrice, targetUnits)` with the invariant `filledBuyerAssets + referralFeeAssets <= maxBuyerAssets`; the fuzzer will find violations whenever `referralFeePct > 0` and `maxBuyerAssets` is set to the bare fill cost.

### Citations

**File:** src/periphery/MidnightBundles.sol (L46-48)
```text
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
