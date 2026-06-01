### Title
Missing `referralFeeRecipient != address(0)` guard causes DoS when `referralFeePct > 0` with zero-address recipient on revert-on-zero-transfer tokens - (`src/periphery/MidnightBundles.sol`)

### Summary
`buyWithUnitsTargetAndWithdrawCollateral` validates only that `referralFeePct < WAD` but never checks that `referralFeeRecipient != address(0)` when `referralFeePct > 0`. When both conditions hold and the loan token follows the standard OpenZeppelin ERC20 pattern (which reverts on `transfer(address(0), ...)`) the unconditional `safeTransfer` at line 103 propagates the token revert, rolling back the entire bundle including all completed takes.

### Finding Description
**Exact code path:**

In `src/periphery/MidnightBundles.sol`, `buyWithUnitsTargetAndWithdrawCollateral` (lines 59–105):

- Line 61: `require(referralFeePct < WAD, PctExceeded())` — the only guard on referral parameters; no check on `referralFeeRecipient`.
- Lines 71–86: take loop executes successfully, accumulating `filledBuyerAssets > 0`.
- Line 102: `referralFeeAssets = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct)` — positive when `referralFeePct > 0` and `filledBuyerAssets > 0`.
- Line 103: `if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets)` — called with `referralFeeRecipient = address(0)`.

In `src/libraries/SafeTransferLib.sol`, `safeTransfer` (lines 12–22):

- Line 15: `token.call(abi.encodeCall(IERC20.transfer, (address(0), referralFeeAssets)))` — the token call.
- Lines 16–19: if `!success`, the library re-reverts with the token's own revert data via inline assembly.

Standard ERC20 implementations (OpenZeppelin ≥ v4) unconditionally revert on `transfer(address(0), ...)`. The `safeTransfer` wrapper faithfully re-reverts, unwinding the entire transaction.

**Attacker-controlled inputs:** `referralFeePct` (any value in `(0, WAD)`) and `referralFeeRecipient = address(0)` — both are plain calldata parameters with no cross-validation.

**Why existing checks fail:** The only referral-related check is `require(referralFeePct < WAD)`. There is no `require(referralFeePct == 0 || referralFeeRecipient != address(0))` guard anywhere in the function or in `SafeTransferLib`.

The same pattern is present in all five bundle functions: `supplyCollateralAndSellWithUnitsTarget` (line 167), `buyWithAssetsTargetAndWithdrawCollateral` (line 239), `supplyCollateralAndSellWithAssetsTarget` (line 306), and `repayAndWithdrawCollateral` (line 347).

### Impact Explanation
The entire bundle transaction reverts at the referral fee transfer step. All state changes from the take loop (debt creation, credit issuance, collateral withdrawals) are rolled back. The taker's intended buy flow is completely DoS-ed. No funds are permanently lost (the revert is atomic), but the operation cannot be completed with this parameter combination against any standard ERC20 loan token.

### Likelihood Explanation
Preconditions are: (1) caller passes `referralFeePct > 0` with `referralFeeRecipient = address(0)`, and (2) the loan token reverts on `transfer(address(0), ...)`. Condition (2) is satisfied by every OpenZeppelin-derived ERC20, which is the dominant implementation. Condition (1) can arise from a frontend bug, an off-by-one in parameter encoding, or a direct contract call. The combination is repeatable and deterministic — every call with these inputs against a standard token will revert.

### Recommendation
Add a combined guard immediately after the existing `PctExceeded` check in each bundle function:

```solidity
require(referralFeePct == 0 || referralFeeRecipient != address(0), InvalidReferral());
```

Alternatively, treat `referralFeeRecipient == address(0)` as "no referral fee" by skipping the transfer unconditionally when the recipient is the zero address, regardless of `referralFeePct`. The former is safer as it surfaces the misconfiguration explicitly.

### Proof of Concept
```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity 0.8.34;

import "forge-std/Test.sol";
// ... standard MidnightBundles test setup ...

function testReferralFeeZeroAddressDoS() public {
    // Setup: standard OZ ERC20 loan token (reverts on transfer to address(0))
    // referralFeePct = 1e17 (10%), referralFeeRecipient = address(0)
    uint256 units = 100e18;
    uint256 referralFeePct = 1e17;

    offers[0].buy = false;
    offers[0].maker = borrower;
    offers[0].receiverIfMakerIsSeller = borrower;
    offers[0].maxUnits = units;
    collateralize(market, borrower, units);

    Take[] memory takes = new Take[](1);
    takes[0] = Take({offer: offers[0], units: units, ratifierData: hex""});

    uint256 price = TickLib.tickToPrice(MAX_TICK);
    uint256 maxBuyerAssets = units.mulDivUp(price, WAD);

    vm.prank(lender);
    // Assert: reverts with the token's zero-address transfer error
    vm.expectRevert(); // e.g. "ERC20: transfer to the zero address"
    midnightBundles.buyWithUnitsTargetAndWithdrawCollateral(
        units,
        maxBuyerAssets,
        lender,
        _noPermit(),
        takes,
        new CollateralWithdrawal[](0),
        address(0),
        referralFeePct,
        address(0) // referralFeeRecipient = address(0) with fee > 0
    );

    // Assert: no state change occurred (takes were rolled back)
    assertEq(midnight.debtOf(id, borrower), 0, "debt must be zero after revert");
    assertEq(loanToken.balanceOf(lender), type(uint256).max, "lender balance unchanged");
}
```

Expected assertion: the call reverts; `midnight.debtOf(id, borrower) == 0` and `loanToken.balanceOf(lender) == type(uint256).max` confirm full rollback. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** src/periphery/MidnightBundles.sol (L59-61)
```text
    ) external {
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
        require(referralFeePct < WAD, PctExceeded());
```

**File:** src/periphery/MidnightBundles.sol (L102-103)
```text
        uint256 referralFeeAssets = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
        if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
```

**File:** src/libraries/SafeTransferLib.sol (L12-21)
```text
    function safeTransfer(address token, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());

        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transfer, (to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
        }
        require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferReturnedFalse());
```
