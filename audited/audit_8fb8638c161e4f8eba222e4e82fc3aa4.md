The exploit path is fully confirmed. Here is the complete analysis:

---

### Title
Fee-on-Transfer Collateral Causes Permanent DoS in `supplyCollateralAndSellWithUnitsTarget` - (`src/periphery/MidnightBundles.sol`)

### Summary
`MidnightBundles.supplyCollateralAndSellWithUnitsTarget` pulls `assets` of a fee-on-transfer collateral token from `msg.sender`, receiving only `assets*(1-fee)`, then immediately calls `Midnight.supplyCollateral` with the original `assets` value. Midnight's `supplyCollateral` attempts to pull the full `assets` from `MidnightBundles` via `safeTransferFrom`, but `MidnightBundles` only holds `assets*(1-fee)`, causing an unconditional revert. The same flaw exists identically in `supplyCollateralAndSellWithAssetsTarget`.

### Finding Description
**Exact code path:**

In `MidnightBundles.supplyCollateralAndSellWithUnitsTarget`, the collateral supply loop is:

```solidity
// MidnightBundles.sol lines 134-140
for (uint256 i; i < collateralSupplies.length; i++) {
    address token = market.collateralParams[collateralSupplies[i].collateralIndex].token;
    pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit); // (A)
    forceApproveMax(token, MIDNIGHT);                                                          // (B)
    IMidnight(MIDNIGHT)
        .supplyCollateral(market, collateralSupplies[i].collateralIndex, collateralSupplies[i].assets, taker); // (C)
}
``` [1](#0-0) 

**(A)** `pullToken` resolves to `SafeTransferLib.safeTransferFrom(token, msg.sender, address(this), assets)`. For a fee-on-transfer token, `MidnightBundles` receives `assets*(1-fee)`, not `assets`. [2](#0-1) 

**(C)** `Midnight.supplyCollateral` is called with the original `assets` (not the reduced amount). Inside Midnight:

```solidity
// Midnight.sol line 533 — state written first
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets);
// ...
// Midnight.sol line 545 — transfer attempted after state write
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
``` [3](#0-2) 

`msg.sender` here is `MidnightBundles`, which holds only `assets*(1-fee)`. The `safeTransferFrom` call for `assets` exceeds `MidnightBundles`'s balance, causing the token to revert. Because the state write at line 533 and the transfer at line 545 are in the same transaction, the revert unwinds everything — the entire `supplyCollateralAndSellWithUnitsTarget` call reverts unconditionally.

**Attacker-controlled inputs:** `collateralSupplies[i].assets` — any nonzero value with a fee-on-transfer collateral token registered in the market.

**Why no existing check stops it:** There is no balance-delta measurement between `pullToken` and `supplyCollateral`. The bundler blindly forwards the caller-supplied `assets` figure to Midnight regardless of what was actually received.

### Impact Explanation
Any user attempting to supply a fee-on-transfer collateral token via `MidnightBundles.supplyCollateralAndSellWithUnitsTarget` (or `supplyCollateralAndSellWithAssetsTarget`) will always receive a revert. The bundler is permanently non-functional for that collateral token. Users must interact with `Midnight.supplyCollateral` directly, bypassing the bundler entirely, losing the atomic bundle guarantee (supply + sell in one tx). [4](#0-3) [5](#0-4) 

### Likelihood Explanation
**Preconditions:**
1. A market exists whose `collateralParams[i].token` is a fee-on-transfer ERC20. This is a permissionless market creation protocol — any market creator can register such a token.
2. A user calls `supplyCollateralAndSellWithUnitsTarget` with a non-empty `collateralSupplies` array for that token.

No privileged action is required. The condition is deterministic and repeatable: every call with a fee-on-transfer collateral token will revert. The attacker does not need to do anything — the victim's own legitimate call triggers the DoS.

### Recommendation
Replace the hardcoded `collateralSupplies[i].assets` passed to `supplyCollateral` with the actual received amount, measured as a balance delta:

```solidity
uint256 balanceBefore = IERC20(token).balanceOf(address(this));
pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
uint256 received = IERC20(token).balanceOf(address(this)) - balanceBefore;
forceApproveMax(token, MIDNIGHT);
IMidnight(MIDNIGHT)
    .supplyCollateral(market, collateralSupplies[i].collateralIndex, received, taker);
```

Apply the same fix to `supplyCollateralAndSellWithAssetsTarget` at lines 271–274. [6](#0-5) 

### Proof of Concept
**Foundry unit test plan:**

```solidity
// FeeOnTransferCollateral: 1% fee on every transferFrom
contract FeeOnTransferToken is ERC20 {
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount / 100;
        super.transferFrom(from, to, amount - fee); // recipient gets amount*(1-fee)
        _burn(from, fee);                           // fee burned
        return true;
    }
}

function testFeeOnTransferCollateralDoS() public {
    // 1. Deploy FeeOnTransferToken, register as collateral in a Midnight market.
    // 2. Deal 1000e18 tokens to borrower; borrower approves MidnightBundles.
    // 3. Construct collateralSupplies = [{collateralIndex: 0, assets: 1000e18}]
    // 4. Construct a valid sell Take[] array.
    // 5. vm.prank(borrower);
    //    vm.expectRevert(); // expect revert due to insufficient balance in MidnightBundles
    //    midnightBundles.supplyCollateralAndSellWithUnitsTarget(
    //        targetUnits, 0, borrower, borrower, collateralSupplies, takes, 0, address(0)
    //    );
    // 6. Assert: midnight.collateral(id, borrower, 0) == 0 (no state change persisted)
    // 7. Assert: FeeOnTransferToken.balanceOf(address(midnightBundles)) == 0
    //    (tokens returned to borrower or burned, not stuck)
}
```

**Expected assertion:** The call reverts (DoS confirmed). No collateral is credited. `MidnightBundles` holds no residual balance. The borrower cannot use the bundler for this collateral token under any input.

### Citations

**File:** src/periphery/MidnightBundles.sol (L117-140)
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
        address loanToken = takes[0].offer.market.loanToken;
        // touchMarket to have the correct settlement fees.
        bytes32 id = IMidnight(MIDNIGHT).touchMarket(takes[0].offer.market);

        Market memory market = takes[0].offer.market;
        for (uint256 i; i < collateralSupplies.length; i++) {
            address token = market.collateralParams[collateralSupplies[i].collateralIndex].token;
            pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
            forceApproveMax(token, MIDNIGHT);
            IMidnight(MIDNIGHT)
                .supplyCollateral(market, collateralSupplies[i].collateralIndex, collateralSupplies[i].assets, taker);
        }
```

**File:** src/periphery/MidnightBundles.sol (L252-275)
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
        address loanToken = takes[0].offer.market.loanToken;
        // touchMarket to have the correct settlement fees.
        bytes32 id = IMidnight(MIDNIGHT).touchMarket(takes[0].offer.market);

        Market memory market = takes[0].offer.market;
        for (uint256 i; i < collateralSupplies.length; i++) {
            address token = market.collateralParams[collateralSupplies[i].collateralIndex].token;
            pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
            forceApproveMax(token, MIDNIGHT);
            IMidnight(MIDNIGHT)
                .supplyCollateral(market, collateralSupplies[i].collateralIndex, collateralSupplies[i].assets, taker);
        }
```

**File:** src/periphery/MidnightBundles.sol (L395-397)
```text
        } else {
            SafeTransferLib.safeTransferFrom(token, from, address(this), amount);
        }
```

**File:** src/Midnight.sol (L533-545)
```text
        _position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets);

        if (oldCollateral == 0 && assets > 0) {
            uint128 newCollateralBitmap = _position.collateralBitmap.setBit(collateralIndex);
            _position.collateralBitmap = newCollateralBitmap;
            require(
                UtilsLib.countBits(newCollateralBitmap) <= MAX_COLLATERALS_PER_BORROWER, TooManyActivatedCollaterals()
            );
        }

        emit EventsLib.SupplyCollateral(msg.sender, id, collateralToken, assets, onBehalf);

        SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
```
