### Title
Fee-on-Transfer Token Incompatibility Causes Inflated Collateral Accounting and Undercollateralized Borrowing — (`src/Midnight.sol`)

### Summary
`Midnight.sol` assumes that the amount of tokens received always equals the amount specified in `transferFrom` calls. For fee-on-transfer tokens, the contract records the full requested amount in its internal accounting while only receiving a lesser amount. This discrepancy is most severe in `supplyCollateral()`, where a borrower's on-chain collateral balance is inflated relative to what the contract actually holds, enabling undercollateralized borrowing and guaranteed bad debt upon liquidation. The same class of accounting drift also affects `repay()` and `flashLoan()`.

---

### Finding Description

**Root cause:** `Midnight.sol` performs all state mutations *before* executing token transfers, and never reconciles the recorded amount against the actual balance change. The `SafeTransferLib` only checks that `transferFrom` returns `true`; it does not verify the net balance delta. [1](#0-0) 

The protocol documents this as a TOKEN SAFETY REQUIREMENT: [2](#0-1) 

However, markets are created **permissionlessly** via `touchMarket()` with no on-chain validation that the supplied loan or collateral token is free of fee-on-transfer behavior: [3](#0-2) 

**Attack path — `supplyCollateral()`:**

The function first writes the full `assets` amount into the position's collateral slot, then pulls tokens: [4](#0-3) 

If the collateral token deducts a fee `f` on transfer, the contract records `assets` but only holds `assets - f`. The borrower's `isHealthy()` check uses the inflated on-chain value, allowing them to take on debt that exceeds the real collateral backing.

**Attack path — `repay()`:**

`withdrawable` is incremented by the full `units` before the transfer executes: [5](#0-4) 

With a fee-on-transfer loan token, the contract receives `units - f` but `withdrawable` grows by `units`. Lenders calling `withdraw()` can collectively drain more loan tokens than the contract actually holds.

**Attack path — `flashLoan()`:**

The contract sends `assets[i]` out and expects exactly `assets[i]` back: [6](#0-5) 

With a fee-on-transfer token, the repayment transfer delivers `assets[i] - f` to the contract, causing a direct, per-call loss from the protocol's reserves.

---

### Impact Explanation

- **`supplyCollateral()` path:** Borrower records `assets` collateral but contract holds `assets - f`. The borrower can borrow against the phantom collateral. On liquidation, `seizedAssets` is computed from the inflated on-chain value; the actual transfer of collateral to the liquidator will either revert (insufficient balance) or succeed while leaving the protocol short, realizing bad debt that is socialized across all lenders in the market.
- **`repay()` path:** `withdrawable` is permanently inflated. Lenders who withdraw early drain real tokens; later lenders or the fee claimer face a shortfall.
- **`flashLoan()` path:** Each flash loan call permanently reduces the protocol's token reserves by the fee amount.

---

### Likelihood Explanation

Markets are created permissionlessly. Any actor can deploy a market using a fee-on-transfer token as the loan or collateral asset. Users who interact with such a market without independently verifying the token's transfer behavior are exposed. The likelihood is realistic wherever the protocol is used with non-standard ERC-20 tokens (e.g., tokens with configurable fees, rebasing tokens with fee modes, or tokens that activate fees post-deployment).

---

### Recommendation

1. **Balance-delta check:** After each inbound `transferFrom`, compare `balanceAfter - balanceBefore` against the expected amount and revert if they differ. This is the standard defense used by protocols like Uniswap V2.
2. **Alternatively**, add an explicit on-chain validation step in `touchMarket()` that performs a self-transfer and asserts the received amount equals the sent amount, rejecting fee-on-transfer tokens at market creation time.

---

### Proof of Concept

**Scenario: Undercollateralized borrowing via `supplyCollateral()` with a 1% fee-on-transfer collateral token.**

1. Deploy a collateral token with a 1% transfer fee.
2. Create a market permissionlessly via `touchMarket()` using this token as collateral (LLTV = 80%).
3. Call `supplyCollateral(market, 0, 1000e18, attacker)`.
   - Contract records `position[id][attacker].collateral[0] = 1000e18`.
   - Contract actually receives `990e18` (1% fee deducted).
4. Call `take()` to borrow against the inflated collateral. `isHealthy()` computes `maxDebt` using `1000e18`, allowing borrowing up to `800e18` units.
5. The real collateral backing is only `990e18`, supporting at most `792e18` units — the position is already undercollateralized by `8e18` units at inception.
6. On liquidation, the contract attempts to `safeTransfer(collateralToken, receiver, seizedAssets)` where `seizedAssets` is derived from the inflated `1000e18` record. If the contract's actual balance is `990e18`, the transfer of the full computed amount either reverts or leaves the protocol insolvent, with bad debt socialized to lenders. [7](#0-6) [8](#0-7)

### Citations

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

**File:** src/Midnight.sol (L133-140)
```text
/// TOKEN SAFETY REQUIREMENTS
/// @dev List of assumptions on tokens that guarantee that Midnight behaves as expected:
/// - It should be ERC-20 compliant, except that it can omit return values on transfer and transferFrom. In particular,
/// it should not revert because a transfer is no-op.
/// - Midnight's balance of the token should only decrease on transfer and transferFrom.
/// - It should not re-enter Midnight on transfer nor transferFrom.
/// - Midnight must send/receive exactly the requested amount on transfers.
/// @dev See LIVENESS for liveness guarantees.
```

**File:** src/Midnight.sol (L508-520)
```text
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
```

**File:** src/Midnight.sol (L524-546)
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
    }
```

**File:** src/Midnight.sol (L696-717)
```text
        SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);

        if (callback != address(0)) {
            require(
                ILiquidateCallback(callback)
                    .onLiquidate(
                        msg.sender,
                        id,
                        market,
                        collateralIndex,
                        seizedAssets,
                        repaidUnits,
                        borrower,
                        receiver,
                        data,
                        badDebt
                    ) == CALLBACK_SUCCESS,
                WrongLiquidateCallbackReturnValue()
            );
        }

        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
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
