### Title
Loan and Collateral Tokens Sent Directly to `Midnight.sol` Are Permanently Irrecoverable — (File: src/Midnight.sol)

---

### Summary

`Midnight.sol` accepts loan tokens and collateral tokens exclusively through protocol functions (`repay`, `supplyCollateral`, `take`). Each of these functions increments specific internal accounting variables before pulling tokens. If a user accidentally sends tokens directly via a plain ERC20 `transfer()` call, those tokens are credited to the contract's raw balance but are never reflected in any tracked accounting slot. Unlike the referenced Incentivizer contract which at least had a `rescue()` function (even if it excluded the underlying token), `Midnight.sol` has **no rescue or recovery mechanism of any kind**. The tokens are permanently irrecoverable.

---

### Finding Description

**Root cause — accounting-only token tracking with no balance-based escape hatch.**

Every legitimate token inflow into `Midnight.sol` is paired with an accounting update:

| Function | Token pulled | Accounting updated |
|---|---|---|
| `repay()` | `loanToken` | `marketState[id].withdrawable += units` |
| `supplyCollateral()` | `collateralToken` | `position[id][onBehalf].collateral[index] += assets` |
| `take()` | `loanToken` (fee portion) | `claimableSettlementFee[loanToken] += fee` | [1](#0-0) [2](#0-1) [3](#0-2) 

Every legitimate token outflow is gated on those same accounting variables:

- `withdraw()` decrements `withdrawable` before calling `safeTransfer` [4](#0-3) 
- `withdrawCollateral()` decrements `position.collateral[index]` before calling `safeTransfer` [5](#0-4) 
- `claimSettlementFee()` decrements `claimableSettlementFee[token]` before calling `safeTransfer` [6](#0-5) 
- `claimContinuousFee()` decrements `continuousFeeCredit` before calling `safeTransfer` [7](#0-6) 

If tokens arrive via a raw ERC20 `transfer()` call, **none of these accounting variables are incremented**. Therefore no withdrawal path can ever reach those tokens.

The `flashLoan()` function sends tokens from the raw contract balance but requires exact repayment of the same amount, so it cannot be used to extract the surplus either. [8](#0-7) 

A search of the entire codebase confirms there is **no `rescue`, `sweep`, `recover`, or emergency-withdrawal function** anywhere in `Midnight.sol` or its interfaces. [9](#0-8) 

---

### Impact Explanation

Any ERC20 token — whether a loan token or a collateral token — sent to `Midnight.sol` via a direct `transfer()` call is **permanently locked** in the contract with zero recovery path. The loss is total and irreversible for the sender. This satisfies the impact category: *"Permanent lock, freeze, or unrecoverable corruption of user/project state."*

---

### Likelihood Explanation

This is a realistic and recurring scenario in DeFi:

- Users repaying debt may call `token.transfer(midnight, amount)` instead of `midnight.repay(...)`.
- Users supplying collateral may call `token.transfer(midnight, amount)` instead of `midnight.supplyCollateral(...)`.
- Wallet UIs, scripts, or integrations that interact with the raw token contract rather than the protocol contract are common sources of this mistake.
- The contract address is publicly known and tokens can be sent to it by anyone at any time.

No privileged access is required. Any normal user can trigger this loss on themselves.

---

### Recommendation

Add a `rescue` function that computes the surplus between the contract's actual ERC20 balance and the sum of all tracked liabilities for that token, and allows a trusted role (e.g., `feeClaimer` or `roleSetter`) to transfer only the surplus to a receiver. This mirrors the fix applied in the referenced pull request 14 for the Incentivizer contract.

For loan tokens, the tracked liability is `marketState[id].withdrawable + claimableSettlementFee[token] + marketState[id].continuousFeeCredit` (summed across all markets using that loan token). For collateral tokens, the tracked liability is the sum of all `position[id][user].collateral[index]` entries for that token.

---

### Proof of Concept

1. Alice has 100 USDC of debt in a Midnight market.
2. Alice intends to repay by calling `midnight.repay(market, 100e6, alice, address(0), "")`.
3. Instead, Alice (or a buggy UI) calls `USDC.transfer(address(midnight), 100e6)`.
4. `Midnight.sol` receives 100 USDC. `marketState[id].withdrawable` is **not** incremented.
5. Alice's debt remains unchanged. The 100 USDC sits in the contract's raw balance.
6. No function in `Midnight.sol` — `withdraw`, `claimSettlementFee`, `claimContinuousFee`, `flashLoan`, or any other — can move these tokens out.
7. The 100 USDC is permanently irrecoverable. [10](#0-9) [11](#0-10)

### Citations

**File:** src/Midnight.sol (L305-310)
```text
    function claimSettlementFee(address token, uint256 amount, address receiver) external {
        require(msg.sender == feeClaimer, OnlyFeeClaimer());
        claimableSettlementFee[token] -= amount;
        emit EventsLib.ClaimSettlementFee(msg.sender, token, amount, receiver);
        SafeTransferLib.safeTransfer(token, receiver, amount);
    }
```

**File:** src/Midnight.sol (L318-324)
```text
        _marketState.continuousFeeCredit -= UtilsLib.toUint128(amount);
        _marketState.totalUnits -= UtilsLib.toUint128(amount);
        _marketState.withdrawable -= UtilsLib.toUint128(amount);

        emit EventsLib.ClaimContinuousFee(msg.sender, id, amount, receiver);

        SafeTransferLib.safeTransfer(market.loanToken, receiver, amount);
```

**File:** src/Midnight.sol (L418-418)
```text
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

**File:** src/Midnight.sol (L494-499)
```text
        _marketState.withdrawable -= UtilsLib.toUint128(units);
        _marketState.totalUnits -= UtilsLib.toUint128(units);

        emit EventsLib.Withdraw(msg.sender, id, units, onBehalf, receiver, pendingFeeDecrease);

        SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
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

**File:** src/Midnight.sol (L561-572)
```text
        uint256 newCollateral = _position.collateral[collateralIndex] - assets;
        _position.collateral[collateralIndex] = UtilsLib.toUint128(newCollateral);

        if (newCollateral == 0 && assets > 0) {
            _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
        }

        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());

        emit EventsLib.WithdrawCollateral(msg.sender, id, collateralToken, assets, onBehalf, receiver);

        SafeTransferLib.safeTransfer(collateralToken, receiver, assets);
```

**File:** src/Midnight.sol (L742-751)
```text
        for (uint256 i = 0; i < tokens.length; i++) {
            SafeTransferLib.safeTransfer(tokens[i], callback, assets[i]);
        }
        require(
            IFlashLoanCallback(callback).onFlashLoan(msg.sender, tokens, assets, data) == CALLBACK_SUCCESS,
            WrongFlashLoanCallbackReturnValue()
        );
        for (uint256 i = 0; i < tokens.length; i++) {
            SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i]);
        }
```

**File:** src/interfaces/IMidnight.sol (L132-184)
```text
    /// MULTICALL ///
    function multicall(bytes[] memory calls) external;

    /// ADMIN FUNCTIONS ///
    function setRoleSetter(address newRoleSetter) external;
    function setFeeSetter(address newFeeSetter) external;
    function setFeeClaimer(address newFeeClaimer) external;
    function setTickSpacingSetter(address newTickSpacingSetter) external;
    function setMarketTickSpacing(bytes32 id, uint256 newTickSpacing) external;
    function setMarketSettlementFee(bytes32 id, uint256 index, uint256 newSettlementFee) external;
    function setDefaultSettlementFee(address loanToken, uint256 index, uint256 newSettlementFee) external;
    function setMarketContinuousFee(bytes32 id, uint256 newContinuousFee) external;
    function setDefaultContinuousFee(address loanToken, uint256 newContinuousFee) external;
    function claimSettlementFee(address token, uint256 amount, address receiver) external;
    function claimContinuousFee(Market memory market, uint256 amount, address receiver) external;

    /// ENTRY-POINTS ///
    function take(Offer memory offer, bytes memory ratifierData, uint256 units, address taker, address receiverIfTakerIsSeller, address takerCallback, bytes memory takerCallbackData) external returns (uint256, uint256);
    function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external;
    function repay(Market memory market, uint256 units, address onBehalf, address callback, bytes memory data) external;
    function supplyCollateral(Market memory market, uint256 collateralIndex, uint256 assets, address onBehalf) external;
    function withdrawCollateral(Market memory market, uint256 collateralIndex, uint256 assets, address onBehalf, address receiver) external;
    function liquidate(Market memory market, uint256 collateralIndex, uint256 seizedAssets, uint256 repaidUnits, address borrower, bool postMaturityMode, address receiver, address callback, bytes memory data) external returns (uint256, uint256);
    function setConsumed(bytes32 group, uint256 amount, address onBehalf) external;
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external;
    function flashLoan(address[] memory tokens, uint256[] memory assets, address callback, bytes memory data) external;
    function touchMarket(Market memory market) external returns (bytes32);

    /// SLASHING AND CONTINUOUS FEE ACCRUAL ///
    function updatePositionView(Market memory market, bytes32 id, address user) external view returns (uint128, uint128, uint128);
    function updatePosition(Market memory market, address user) external returns (uint128, uint128, uint128);

    /// OTHER VIEW FUNCTIONS ///
    function lastLossFactor(bytes32 id, address user) external view returns (uint128);
    function collateralBitmap(bytes32 id, address user) external view returns (uint128);
    function collateral(bytes32 id, address user, uint256 index) external view returns (uint128);
    function toId(Market memory market) external view returns (bytes32);
    function toMarket(bytes32 id) external view returns (Market memory);
    function creditOf(bytes32 id, address user) external view returns (uint128);
    function debtOf(bytes32 id, address user) external view returns (uint128);
    function totalUnits(bytes32 id) external view returns (uint128);
    function lossFactor(bytes32 id) external view returns (uint128);
    function tickSpacing(bytes32 id) external view returns (uint8);
    function withdrawable(bytes32 id) external view returns (uint128);
    function settlementFeeCbps(bytes32 id) external view returns (uint16[7] memory);
    function continuousFee(bytes32 id) external view returns (uint32);
    function continuousFeeCredit(bytes32 id) external view returns (uint128);
    function pendingFee(bytes32 id, address user) external view returns (uint128);
    function lastAccrual(bytes32 id, address user) external view returns (uint128);
    function liquidationLocked(bytes32 id, address user) external view returns (bool);
    function isHealthy(Market memory market, bytes32 id, address borrower) external view returns (bool);
    function settlementFee(bytes32 id, uint256 timeToMaturity) external view returns (uint256);
    // forgefmt: disable-end
```
