### Title
Fee-on-Transfer Collateral Token Causes `supplyCollateralAndSellWithUnitsTarget` Bundle to Always Revert - (`src/periphery/MidnightBundles.sol`)

### Summary
`MidnightBundles.supplyCollateralAndSellWithUnitsTarget` pulls `collateralSupplies[i].assets` from the caller via `pullToken`, but when the collateral token charges a transfer fee the bundler receives only `assets*(1-f)`. The bundler then passes the original `assets` value to `Midnight.supplyCollateral`, which calls `SafeTransferLib.safeTransferFrom(collateralToken, bundler, midnight, assets)`. Because the bundler holds only `assets*(1-f) < assets`, this transfer reverts, causing the entire bundle to revert and permanently DoS-ing the user's intended collateral-supply-and-borrow flow.

### Finding Description

**Exact code path:**

1. `MidnightBundles.supplyCollateralAndSellWithUnitsTarget` (lines 134–139):
   ```solidity
   address token = market.collateralParams[collateralSupplies[i].collateralIndex].token;
   pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
   forceApproveMax(token, MIDNIGHT);
   IMidnight(MIDNIGHT)
       .supplyCollateral(market, collateralSupplies[i].collateralIndex, collateralSupplies[i].assets, taker);
   ```
   `pullToken` (line 396) calls `SafeTransferLib.safeTransferFrom(token, from, address(this), amount)`. For a fee-on-transfer token with fee rate `f`, the bundler's balance increases by only `amount*(1-f)`, not `amount`.

2. `Midnight.supplyCollateral` (line 545):
   ```solidity
   SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
   ```
   Here `msg.sender` is the bundler and `assets` is the original nominal value. The bundler holds `assets*(1-f)` but the call requests `assets`. The ERC-20 `transferFrom` reverts with insufficient balance.

**Root cause:** The bundler uses the caller-supplied `assets` parameter both as the pull amount and as the forward amount to Midnight, with no accounting for tokens lost to transfer fees between the two steps.

**Attacker-controlled inputs:** `collateralSupplies[i].assets` — any positive value with a fee-on-transfer collateral token triggers the revert.

**Why existing checks fail:**
- `SafeTransferLib.safeTransferFrom` (lines 24–34) only checks call success and the boolean return value; it does not verify the recipient's balance delta.
- `forceApproveMax` (lines 371–375) grants unlimited allowance to Midnight, so the approval is not the bottleneck — the bundler's actual token balance is.
- There is no pre/post balance check in the bundler between `pullToken` and `supplyCollateral`.
- No explicit exclusion of fee-on-transfer tokens appears anywhere in `Midnight.sol` or `MidnightBundles.sol` (the header at line 23 of `MidnightBundles.sol` says "Inherits the token safety requirements of Midnight" but no such requirement is documented in `Midnight.sol`).

### Impact Explanation
Every call to `supplyCollateralAndSellWithUnitsTarget` (and identically `supplyCollateralAndSellWithAssetsTarget`) with a fee-on-transfer collateral token reverts unconditionally. The user has already approved the bundler to spend their tokens; the fee is deducted from their balance; yet the bundle fails and no collateral is supplied and no borrow is executed. The user cannot use the bundler to open a leveraged position in any market whose collateral token charges a transfer fee, regardless of how they parameterize the call.

### Likelihood Explanation
**Preconditions:** (1) A market exists whose `collateralParams[i].token` is a fee-on-transfer ERC-20. Market creation is permissionless, so any unprivileged actor can create such a market. (2) A user calls the bundler with `collateralSupplies` referencing that token. Both conditions are reachable without any privileged action. The failure is deterministic and repeatable on every invocation.

### Recommendation
Replace the nominal `collateralSupplies[i].assets` forwarded to `supplyCollateral` with the actual amount received by the bundler. Measure the bundler's balance before and after `pullToken` and pass the delta:

```solidity
uint256 balanceBefore = IERC20(token).balanceOf(address(this));
pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
uint256 received = IERC20(token).balanceOf(address(this)) - balanceBefore;
forceApproveMax(token, MIDNIGHT);
IMidnight(MIDNIGHT).supplyCollateral(market, collateralSupplies[i].collateralIndex, received, taker);
```

Apply the same fix to `supplyCollateralAndSellWithAssetsTarget` (lines 270–274). Note that `received` will also be the amount credited to the taker's collateral on Midnight, which callers must account for when sizing their borrow.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import "forge-std/Test.sol";
// ... standard Midnight test imports ...

contract FeeOnTransferCollateral is ERC20 {
    uint256 public constant FEE_BPS = 100; // 1% fee
    constructor() ERC20("FOT", "FOT") {}
    function mint(address to, uint256 amt) external { _mint(to, amt); }
    function _transfer(address from, address to, uint256 amount) internal override {
        uint256 fee = amount * FEE_BPS / 10000;
        super._transfer(from, address(0xdead), fee); // burn fee
        super._transfer(from, to, amount - fee);
    }
}

contract FeeOnTransferBundlerTest is Test {
    // Setup: deploy Midnight + MidnightBundles, create market with FOT collateral token
    // Fund borrower with FOT tokens, borrower approves bundler

    function testSupplyCollateralFOTReverts() public {
        uint256 assets = 1000e18;
        // borrower has assets FOT, approved bundler for assets
        
        CollateralSupply[] memory supplies = new CollateralSupply[](1);
        supplies[0] = CollateralSupply({
            collateralIndex: 0,
            assets: assets,
            permit: _noPermit()
        });

        vm.prank(borrower);
        vm.expectRevert(); // ERC20: transfer amount exceeds balance (or similar)
        midnightBundles.supplyCollateralAndSellWithUnitsTarget(
            targetUnits, 0, borrower, borrower, supplies, takes, 0, address(0)
        );

        // Assert: no collateral was credited to borrower on Midnight
        assertEq(midnight.collateral(id, borrower, 0), 0, "no collateral credited");
        // Assert: bundler holds no residual tokens
        assertEq(fotToken.balanceOf(address(midnightBundles)), 0, "no bundler residual");
    }
}
```

**Expected assertion:** `vm.expectRevert()` passes because `Midnight.supplyCollateral` reverts when attempting `safeTransferFrom(fotToken, bundler, midnight, assets)` with the bundler holding only `assets * 0.99`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/periphery/MidnightBundles.sol (L134-139)
```text
        for (uint256 i; i < collateralSupplies.length; i++) {
            address token = market.collateralParams[collateralSupplies[i].collateralIndex].token;
            pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
            forceApproveMax(token, MIDNIGHT);
            IMidnight(MIDNIGHT)
                .supplyCollateral(market, collateralSupplies[i].collateralIndex, collateralSupplies[i].assets, taker);
```

**File:** src/periphery/MidnightBundles.sol (L371-375)
```text
    function forceApproveMax(address token, address spender) internal {
        if (IERC20(token).allowance(address(this), spender) >= type(uint96).max / 2) return;
        safeApprove(token, spender, 0);
        safeApprove(token, spender, type(uint256).max);
    }
```

**File:** src/periphery/MidnightBundles.sol (L377-397)
```text
    /// @dev Pulls `amount` of `token` from `from` to this bundler, optionally using ERC2612 or Permit2.
    function pullToken(address token, address from, uint256 amount, TokenPermit memory permit) internal {
        if (permit.kind == PermitKind.ERC2612) {
            (uint256 deadline, uint8 v, bytes32 r, bytes32 s) =
                abi.decode(permit.data, (uint256, uint8, bytes32, bytes32));
            // Tolerate revert: a third party may have already consumed the permit.
            try IERC20Permit(token).permit(from, address(this), amount, deadline, v, r, s) {} catch {}
            SafeTransferLib.safeTransferFrom(token, from, address(this), amount);
        } else if (permit.kind == PermitKind.Permit2) {
            (uint256 nonce, uint256 deadline, bytes memory signature) =
                abi.decode(permit.data, (uint256, uint256, bytes));
            IPermit2(PERMIT2)
                .permitTransferFrom(
                    IPermit2.PermitTransferFrom(IPermit2.TokenPermissions(token, amount), nonce, deadline),
                    IPermit2.SignatureTransferDetails(address(this), amount),
                    from,
                    signature
                );
        } else {
            SafeTransferLib.safeTransferFrom(token, from, address(this), amount);
        }
```

**File:** src/Midnight.sol (L524-545)
```text
    function supplyCollateral(Market memory market, uint256 collateralIndex, uint256 assets, address onBehalf)
        external
    {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        address collateralToken = market.collateralParams[collateralIndex].token;

        Position storage _position = position[id][onBehalf];
        uint256 oldCollateral = _position.collateral[collateralIndex];
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

**File:** src/libraries/SafeTransferLib.sol (L24-34)
```text
    function safeTransferFrom(address token, address from, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());

        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transferFrom, (from, to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
        }
        require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferFromReturnedFalse());
    }
```
