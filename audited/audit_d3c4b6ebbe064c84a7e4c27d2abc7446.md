### Title
Fee-on-transfer loan token causes `repayAndWithdrawCollateral` to revert due to balance shortfall - (File: src/periphery/MidnightBundles.sol)

### Summary
`repayAndWithdrawCollateral` computes `units = assets - referralFeeAssets` from the caller-supplied nominal `assets` value before pulling tokens, but `pullToken` delivers only `assets - FOT_fee` to the bundler when the loan token charges a transfer fee. When `FOT_fee > referralFeeAssets` (trivially satisfied when `referralFeePct = 0`), the subsequent `safeTransferFrom(loanToken, bundler, midnight, units)` inside `Midnight.repay` reverts because the bundler's actual balance is insufficient. No existing check detects or prevents this shortfall.

### Finding Description
**Exact code path:**

`repayAndWithdrawCollateral` (MidnightBundles.sol lines 329–334):
```
referralFeeAssets = assets.mulDivDown(referralFeePct, WAD)   // computed from nominal assets
units             = assets - referralFeeAssets                // also from nominal assets
pullToken(loanToken, msg.sender, assets, ...)                 // bundler receives assets - FOT_fee
forceApproveMax(loanToken, MIDNIGHT)
IMidnight(MIDNIGHT).repay(market, units, onBehalf, address(0), "")
```

Inside `Midnight.repay` (Midnight.sol line 511, 520):
```
address payer = callback != address(0) ? callback : msg.sender;  // payer = bundler
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
```

`pullToken` unconditionally calls `SafeTransferLib.safeTransferFrom(token, from, address(this), amount)` (MidnightBundles.sol line 396), which succeeds at the ERC-20 level but delivers only `assets - FOT_fee` to the bundler. The bundler's balance is therefore `assets - FOT_fee`, while `units = assets - referralFeeAssets`. The transfer in `repay` requires `units` tokens from the bundler. The condition for revert is:

```
units > bundler_balance
assets - referralFeeAssets > assets - FOT_fee
FOT_fee > referralFeeAssets
```

With `referralFeePct = 0`: `referralFeeAssets = 0`, `units = assets`, bundler holds `assets - FOT_fee`. Any positive FOT fee causes the revert. With nonzero `referralFeePct`: the revert occurs whenever the token's fee rate exceeds `referralFeePct / WAD`.

**Attacker-controlled inputs:** The borrower (or any authorized repayer) calls `repayAndWithdrawCollateral` on a market whose `loanToken` is a fee-on-transfer token, with `referralFeePct = 0` (or any value below the token's fee rate). No special privilege is required; the caller is the position owner.

**Why existing checks fail:** `touchMarket` validates only collateral params, LLTV, and maturity — no FOT exclusion. The bundler has no post-pull balance check. `forceApproveMax` grants unlimited allowance, so the allowance is not the limiting factor; the bundler's actual token balance is.

### Impact Explanation
Every call to `repayAndWithdrawCollateral` on a market with a fee-on-transfer loan token reverts when `FOT_fee > referralFeeAssets`. With `referralFeePct = 0` (the common case), any positive transfer fee causes a revert. The bundler's combined repay-and-withdraw-collateral flow is permanently broken for such markets; borrowers cannot use this path to repay debt and reclaim collateral atomically.

### Likelihood Explanation
Fee-on-transfer tokens (e.g., tokens with deflationary mechanics or protocol-level transfer taxes) are a known ERC-20 variant. Any market creator can deploy a market with such a loan token — `touchMarket` imposes no restriction. The failure is deterministic and repeatable: every invocation with `referralFeePct < FOT_rate * WAD` reverts. The precondition (FOT loan token market exists, borrower calls the bundler) is fully user-reachable without any privileged action.

### Recommendation
After `pullToken`, measure the bundler's actual received balance and use that as the basis for `units`:

```solidity
uint256 balanceBefore = IERC20(loanToken).balanceOf(address(this));
pullToken(loanToken, msg.sender, assets, loanTokenPermit);
uint256 received = IERC20(loanToken).balanceOf(address(this)) - balanceBefore;
uint256 referralFeeAssets = received.mulDivDown(referralFeePct, WAD);
uint256 units = received - referralFeeAssets;
```

This ensures `units` never exceeds the bundler's actual balance, making the flow safe for fee-on-transfer tokens.

### Proof of Concept
```solidity
// Foundry unit test
contract FotToken is ERC20 {
    // 1% fee on every transfer/transferFrom
    function _transfer(address from, address to, uint256 amount) internal override {
        uint256 fee = amount / 100;
        super._transfer(from, address(0xdead), fee);
        super._transfer(from, to, amount - fee);
    }
}

function testRepayAndWithdrawCollateralFotReverts() public {
    FotToken fotToken = new FotToken();
    // Create market with fotToken as loanToken, set up borrower position...
    uint256 assets = 1000e18;
    // referralFeePct = 0 → units = assets = 1000e18
    // pullToken delivers 990e18 to bundler (1% fee)
    // repay tries safeTransferFrom(bundler, midnight, 1000e18) → REVERT (bundler has 990e18)
    vm.expectRevert();
    midnightBundles.repayAndWithdrawCollateral(
        market, assets, borrower, _noPermit(),
        new CollateralWithdrawal[](0), address(0), 0, address(0)
    );
    // Assert: debt unchanged, collateral unchanged
    assertEq(midnight.debtOf(id, borrower), initialDebt);
}
```

Expected assertion: the call reverts; debt and collateral remain unchanged, confirming the repay+withdraw path is blocked for FOT loan tokens. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** src/Midnight.sol (L511-520)
```text
        address payer = callback != address(0) ? callback : msg.sender;
        emit EventsLib.Repay(msg.sender, id, units, onBehalf, payer);

        if (callback != address(0)) {
            require(
                IRepayCallback(callback).onRepay(id, market, units, onBehalf, data) == CALLBACK_SUCCESS,
                WrongRepayCallbackReturnValue()
            );
        }
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
```

**File:** src/Midnight.sol (L755-791)
```text
    function touchMarket(Market memory market) public returns (bytes32) {
        bytes32 id = toId(market);
        if (marketState[id].tickSpacing == 0) {
            require(market.maturity <= block.timestamp + 100 * 365 days, MaturityTooFar());
            require(market.collateralParams.length > 0, NoCollateralParams());
            require(market.collateralParams.length <= MAX_COLLATERALS, TooManyCollateralParams());
            address previousCollateralToken;
            for (uint256 i = 0; i < market.collateralParams.length; i++) {
                address collateralToken = market.collateralParams[i].token;
                require(collateralToken > previousCollateralToken, CollateralParamsNotSorted());
                uint256 lltv = market.collateralParams[i].lltv;
                require(isLltvAllowed(lltv), LltvNotAllowed());
                require(
                    market.collateralParams[i].maxLif == maxLif(lltv, LIQUIDATION_CURSOR_LOW)
                        || market.collateralParams[i].maxLif == maxLif(lltv, LIQUIDATION_CURSOR_HIGH),
                    InvalidMaxLif()
                );
                previousCollateralToken = collateralToken;
            }

            MarketState storage _marketState = marketState[id];
            _marketState.tickSpacing = DEFAULT_TICK_SPACING;
            uint16[7] memory _defaultSettlementFeeCbp = defaultSettlementFeeCbp[market.loanToken];
            _marketState.settlementFeeCbp0 = _defaultSettlementFeeCbp[0];
            _marketState.settlementFeeCbp1 = _defaultSettlementFeeCbp[1];
            _marketState.settlementFeeCbp2 = _defaultSettlementFeeCbp[2];
            _marketState.settlementFeeCbp3 = _defaultSettlementFeeCbp[3];
            _marketState.settlementFeeCbp4 = _defaultSettlementFeeCbp[4];
            _marketState.settlementFeeCbp5 = _defaultSettlementFeeCbp[5];
            _marketState.settlementFeeCbp6 = _defaultSettlementFeeCbp[6];
            _marketState.continuousFee = defaultContinuousFee[market.loanToken];
            IdLib.storeInCode(market, INITIAL_CHAIN_ID);

            emit EventsLib.MarketCreated(market, id);
        }
        return id;
    }
```
