Audit Report

## Title
Arithmetic underflow in `buyWithUnitsTargetAndWithdrawCollateral` when `filledBuyerAssets == maxBuyerAssets` and `referralFeePct > 0` - (File: src/periphery/MidnightBundles.sol)

## Summary
`buyWithUnitsTargetAndWithdrawCollateral` pulls `maxBuyerAssets` from the caller upfront, then lets the takes loop consume up to `maxBuyerAssets` of that balance. When the loop fills exactly `maxBuyerAssets` and `referralFeePct > 0`, the bundler holds zero remaining tokens. The subsequent referral transfer at line 103 fails (ERC20 balance insufficient), and the change-return subtraction at line 104 underflows under Solidity 0.8.34 checked arithmetic, causing an unconditional revert. No funds are permanently lost due to atomicity, but the function is completely unusable for this natural input combination.

## Finding Description

**Exact code path:**

`pullToken` at line 66 transfers `maxBuyerAssets` into the bundler. [1](#0-0) 

The takes loop at lines 71–86 causes MIDNIGHT to pull `filledBuyerAssets` from the bundler via the `type(uint256).max` approval granted at line 67. After the loop the bundler holds exactly `maxBuyerAssets - filledBuyerAssets` tokens. [2](#0-1) 

The referral fee is then computed and paid out: [3](#0-2) 

`mulDivDown` is a plain `(x * y) / d` with no overflow guard beyond Solidity's checked arithmetic: [4](#0-3) 

**Root cause — missing budget check:**

The only guard on `referralFeePct` is `require(referralFeePct < WAD)` at line 61. There is no check that `maxBuyerAssets >= filledBuyerAssets + referralFeeAssets` before the transfers execute. [5](#0-4) 

**Exploit flow:**

1. Caller invokes `buyWithUnitsTargetAndWithdrawCollateral` with `referralFeePct = p` (any value in `[1, WAD-1]`) and `maxBuyerAssets = M`.
2. `pullToken` moves `M` tokens into the bundler.
3. Takes consume exactly `M` tokens (`filledBuyerAssets = M`). Bundler balance = 0.
4. `referralFeeAssets = M * p / (WAD - p) > 0`.
5. Line 103: `safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets)` — bundler has 0 tokens → ERC20 reverts.
6. Even if line 103 were skipped, line 104: `M - M - referralFeeAssets = 0 - referralFeeAssets` → Solidity 0.8.34 checked underflow → revert.

**Why existing checks fail:**

The `PctExceeded` guard only rejects `referralFeePct >= WAD`; it does not bound the fee relative to the available buffer `maxBuyerAssets - filledBuyerAssets`. The takes loop has no cap tied to `maxBuyerAssets` minus a referral reserve. The sibling function `buyWithAssetsTargetAndWithdrawCollateral` avoids this by computing the referral budget upfront and subtracting it from the fill target before takes run, but `buyWithUnitsTargetAndWithdrawCollateral` has no equivalent pre-deduction. [6](#0-5) 

## Impact Explanation

Any call to `buyWithUnitsTargetAndWithdrawCollateral` with `referralFeePct > 0` where the offers consume exactly `maxBuyerAssets` reverts unconditionally. Because the transaction reverts atomically, no tokens are permanently frozen. However, the buy-and-collateral-withdrawal path is rendered completely unusable for this input combination. A taker who sets `maxBuyerAssets` to a tight bound (the natural usage when the caller has pre-computed the expected fill cost) and enables a referral fee will always revert if the market fills to the limit, with no way to complete the operation at that fee level without raising `maxBuyerAssets` beyond the actual fill cost — defeating the purpose of the parameter.

## Likelihood Explanation

The precondition (`filledBuyerAssets == maxBuyerAssets`) is reachable whenever a taker sets `maxBuyerAssets` to exactly the expected fill cost, which is the natural usage pattern for a caller who has pre-computed the price. It is also reachable whenever a single offer's fill rounds up to exactly consume the remaining budget. The condition is not attacker-gated: the taker triggers it on themselves by combining a non-zero `referralFeePct` with a tight `maxBuyerAssets`. It is repeatable for any such parameter combination and is not dependent on oracle values, governance, or privileged state.

## Recommendation

Mirror the approach used in `buyWithAssetsTargetAndWithdrawCollateral`: pre-compute the referral fee budget before the takes loop and subtract it from the available fill budget. Concretely, before the loop in `buyWithUnitsTargetAndWithdrawCollateral`, compute a `referralReserve` from `maxBuyerAssets` and cap `filledBuyerAssets` to `maxBuyerAssets - referralReserve`. Alternatively, add a post-loop check: `require(maxBuyerAssets >= filledBuyerAssets + referralFeeAssets, InsufficientBudget())` before executing either transfer. [3](#0-2) 

## Proof of Concept

**Minimal unit test plan:**

1. Deploy `MidnightBundles` against a mock `IMidnight` that, on `take`, pulls exactly `unitsToTake`-worth of buyer assets from the bundler and returns `(resBuyerAssets, 0)` where `resBuyerAssets == maxBuyerAssets`.
2. Mint `maxBuyerAssets` of `loanToken` to the caller and approve the bundler.
3. Call `buyWithUnitsTargetAndWithdrawCollateral` with `referralFeePct = 1e16` (1%) and `maxBuyerAssets = M` such that the mock fills exactly `M`.
4. Assert the call reverts (either ERC20 insufficient balance at line 103 or arithmetic underflow at line 104).
5. Confirm the same call with `maxBuyerAssets = M + 1` (leaving 1 wei buffer) also reverts because `1 - referralFeeAssets` still underflows, demonstrating the buffer must be at least `referralFeeAssets`. [7](#0-6)

### Citations

**File:** src/periphery/MidnightBundles.sol (L49-105)
```text
    function buyWithUnitsTargetAndWithdrawCollateral(
        uint256 targetUnits,
        uint256 maxBuyerAssets,
        address taker,
        TokenPermit memory loanTokenPermit,
        Take[] memory takes,
        CollateralWithdrawal[] memory collateralWithdrawals,
        address collateralReceiver,
        uint256 referralFeePct,
        address referralFeeRecipient
    ) external {
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
        require(referralFeePct < WAD, PctExceeded());
        address loanToken = takes[0].offer.market.loanToken;
        // touchMarket to have the correct settlement fees.
        bytes32 id = IMidnight(MIDNIGHT).touchMarket(takes[0].offer.market);

        pullToken(loanToken, msg.sender, maxBuyerAssets, loanTokenPermit);
        forceApproveMax(loanToken, MIDNIGHT);

        uint256 filledUnits;
        uint256 filledBuyerAssets;
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

        require(filledUnits == targetUnits, OutOfOffers());

        Market memory market = takes[0].offer.market;
        for (uint256 i; i < collateralWithdrawals.length; i++) {
            IMidnight(MIDNIGHT)
                .withdrawCollateral(
                    market,
                    collateralWithdrawals[i].collateralIndex,
                    collateralWithdrawals[i].assets,
                    taker,
                    collateralReceiver
                );
        }

        uint256 referralFeeAssets = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
        if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
        SafeTransferLib.safeTransfer(loanToken, msg.sender, maxBuyerAssets - filledBuyerAssets - referralFeeAssets);
    }
```

**File:** src/periphery/MidnightBundles.sol (L200-201)
```text
        uint256 referralFeeAssets = targetBuyerAssets.mulDivDown(referralFeePct, WAD);
        uint256 targetFilledBuyerAssets = targetBuyerAssets - referralFeeAssets;
```

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```
