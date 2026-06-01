### Title
Fee-on-transfer loanToken causes `repayAndWithdrawCollateral` to always revert due to bundler balance shortfall - (File: src/periphery/MidnightBundles.sol)

### Summary

`repayAndWithdrawCollateral` computes `units = assets - referralFeeAssets` from the caller-supplied `assets` parameter, then pulls `assets` from `msg.sender` via `pullToken`. When `loanToken` is a fee-on-transfer token, the bundler receives `assets - fee` rather than `assets`, but subsequently instructs Midnight to pull the full `units` from the bundler. Because `units > bundler_balance` (when `referralFeeAssets < fee`), the `safeTransferFrom` inside `Midnight.repay` reverts, permanently blocking the operation.

### Finding Description

**Exact code path:**

In `src/periphery/MidnightBundles.sol` lines 329–334:

```solidity
uint256 referralFeeAssets = assets.mulDivDown(referralFeePct, WAD);
uint256 units = assets - referralFeeAssets;          // computed from nominal `assets`
pullToken(loanToken, msg.sender, assets, loanTokenPermit); // bundler receives assets - fee
forceApproveMax(loanToken, MIDNIGHT);
IMidnight(MIDNIGHT).repay(market, units, onBehalf, address(0), ""); // Midnight pulls `units`
```

In `src/Midnight.sol` line 520, `repay` with `callback = address(0)` sets `payer = msg.sender` (the bundler) and executes:

```solidity
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
```

**Root cause:** `units` is derived from the nominal `assets` argument, not from the actual post-transfer balance received by the bundler. There is no balance snapshot before/after `pullToken` to account for transfer fees.

**Exploit flow:**
1. Attacker (borrower) calls `repayAndWithdrawCollateral` with a fee-on-transfer `loanToken`, `referralFeePct = 0`, and `assets = D` (their debt).
2. `units = D - 0 = D`.
3. `pullToken` transfers `D` from borrower; bundler receives `D - fee`.
4. `forceApproveMax` grants Midnight unlimited allowance.
5. `Midnight.repay(market, D, ...)` calls `safeTransferFrom(loanToken, bundler, midnight, D)`.
6. Bundler balance is `D - fee < D` → ERC20 `transferFrom` reverts → entire call reverts.

**Existing protections reviewed:**
- `require(referralFeePct < WAD)` — does not help; the bug exists even at `referralFeePct = 0`.
- `forceApproveMax` — approval is not the bottleneck; actual token balance is.
- No balance-before/after check exists anywhere in the function.
- No fee-on-transfer guard or slippage parameter exists.

### Impact Explanation

Any borrower using `repayAndWithdrawCollateral` with a fee-on-transfer loan token (and `referralFeePct` small enough that `referralFeeAssets < transferFee`) will have their repayment permanently blocked through this bundler. The call always reverts at `Midnight.repay`, meaning the borrower cannot reduce their debt or withdraw collateral via this path. The bundler is rendered non-functional for that token type.

### Likelihood Explanation

Fee-on-transfer tokens are a well-established ERC20 pattern (e.g., tokens with deflationary mechanics). Any market created with such a token as `loanToken` triggers this bug on every `repayAndWithdrawCollateral` call with `referralFeePct = 0`. The precondition is entirely attacker-reachable: the borrower controls `assets`, `referralFeePct`, and the market selection. No privileged access is required. The bug is 100% repeatable for any fee-on-transfer loanToken market.

### Recommendation

Measure the actual received balance after `pullToken` and use that to compute `units`:

```solidity
uint256 balanceBefore = IERC20(loanToken).balanceOf(address(this));
pullToken(loanToken, msg.sender, assets, loanTokenPermit);
uint256 received = IERC20(loanToken).balanceOf(address(this)) - balanceBefore;

uint256 referralFeeAssets = received.mulDivDown(referralFeePct, WAD);
uint256 units = received - referralFeeAssets;
forceApproveMax(loanToken, MIDNIGHT);
IMidnight(MIDNIGHT).repay(market, units, onBehalf, address(0), "");
```

This ensures `units` never exceeds the bundler's actual balance regardless of transfer fees.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";

// FeeToken: 1% fee on every transfer
contract FeeOnTransferToken is ERC20 {
    constructor() ERC20("FeeToken", "FEE", 18) {}
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount / 100; // 1% fee
        super.transferFrom(from, to, amount - fee);
        _burn(from, fee); // or send to fee collector
        return true;
    }
}

contract FeeOnTransferRepayTest is Test {
    // Setup: deploy FeeOnTransferToken as loanToken, create market,
    // deploy MidnightBundles, have borrower take on debt D.

    function testRepayRevertsWithFeeToken() public {
        // 1. Deploy FeeOnTransferToken as loanToken
        // 2. Create Midnight market with feeToken as loanToken
        // 3. Borrower accumulates debt D via supplyCollateral + sell
        // 4. Deal borrower D feeTokens; borrower approves bundler for D
        // 5. Call repayAndWithdrawCollateral(market, D, borrower, noPermit, [], addr(0), 0, addr(0))
        // Expected: REVERT (bundler holds D - fee, Midnight tries to pull D)
        vm.expectRevert(); // safeTransferFrom fails: insufficient bundler balance
        midnightBundles.repayAndWithdrawCollateral(
            market, D, borrower, noPermit, new CollateralWithdrawal[](0), address(0), 0, address(0)
        );
        // Assert: borrower debt unchanged, bundler balance = D - fee (tokens stuck)
        assertEq(midnight.debtOf(id, borrower), D, "debt must be unchanged");
    }
}
```

**Key assertions:**
- The call reverts (no successful repayment).
- `midnight.debtOf(id, borrower)` remains `D` (debt not reduced).
- Bundler holds `D - fee` tokens after revert (tokens are not returned since the whole tx reverts, so borrower loses nothing — but the operation is blocked). [1](#0-0) [2](#0-1)

### Citations

**File:** src/periphery/MidnightBundles.sol (L328-334)
```text
        address loanToken = market.loanToken;
        uint256 referralFeeAssets = assets.mulDivDown(referralFeePct, WAD);
        uint256 units = assets - referralFeeAssets;
        pullToken(loanToken, msg.sender, assets, loanTokenPermit);
        forceApproveMax(loanToken, MIDNIGHT);

        IMidnight(MIDNIGHT).repay(market, units, onBehalf, address(0), "");
```

**File:** src/Midnight.sol (L502-521)
```text
    function repay(Market memory market, uint256 units, address onBehalf, address callback, bytes calldata data)
        external
    {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);

        position[id][onBehalf].debt -= UtilsLib.toUint128(units);
        marketState[id].withdrawable += UtilsLib.toUint128(units);

        address payer = callback != address(0) ? callback : msg.sender;
        emit EventsLib.Repay(msg.sender, id, units, onBehalf, payer);

        if (callback != address(0)) {
            require(
                IRepayCallback(callback).onRepay(id, market, units, onBehalf, data) == CALLBACK_SUCCESS,
                WrongRepayCallbackReturnValue()
            );
        }
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
    }
```
