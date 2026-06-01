Audit Report

## Title
Fee-on-Transfer Collateral Token Inflates Stored Collateral, Enabling Uncollateralized Borrowing - (File: src/Midnight.sol)

## Summary
`supplyCollateral` writes `_position.collateral[collateralIndex]` and sets the `collateralBitmap` bit before calling `SafeTransferLib.safeTransferFrom`, which only validates the boolean return value and never checks the actual balance received. A fee-on-transfer token that returns `true` from `transferFrom` while delivering zero tokens leaves the protocol with phantom collateral in storage. `isHealthy` computes `maxDebt` from that phantom stored value, allowing an attacker to borrow real loan tokens against zero actual collateral, creating immediate bad debt.

## Finding Description
**Root cause — `supplyCollateral` (`src/Midnight.sol` lines 531–545):**

State is mutated before the transfer executes: [1](#0-0) 

`_position.collateral[collateralIndex]` is set to `oldCollateral + assets` and the `collateralBitmap` bit is set at lines 533–537, then `SafeTransferLib.safeTransferFrom` is called at line 545.

**`SafeTransferLib.safeTransferFrom` (`src/libraries/SafeTransferLib.sol` lines 24–34):** [2](#0-1) 

Only two checks exist: call reverted → revert, return value is `false` → revert. No balance-delta check. A 100%-fee-on-transfer token returns `true` and emits a `Transfer` event for `assets` while moving 0 tokens to `address(this)`. Both checks pass.

**`isHealthy` (`src/Midnight.sol` lines 944–959):** [3](#0-2) 

`_position.collateral[i]` is the phantom stored value. With a valid oracle price and any allowed LLTV, `maxDebt` is positive and `isHealthy` returns `true`.

**Health gate in `take` (`src/Midnight.sol` line 476):** [4](#0-3) 

This is the only post-borrow health check for the seller. It passes because `isHealthy` uses phantom collateral.

**`touchMarket` (`src/Midnight.sol` lines 755–791):** [5](#0-4) 

Permissionless; validates only LLTV tier, maxLif, sort order, and count. No token whitelist or fee-on-transfer guard exists.

**Exploit flow:**
1. Attacker deploys `FeeToken` — `transferFrom` returns `true`, transfers 0 tokens.
2. Attacker calls `touchMarket(market)` with `FeeToken` as the sole collateral token and a valid LLTV/maxLif pair.
3. A victim lender places a buy offer (supplies loan tokens) in this market.
4. Attacker calls `supplyCollateral(market, 0, 1e18, attacker)`: `_position.collateral[0]` becomes `1e18`, bit 0 of `collateralBitmap` is set, `FeeToken.transferFrom` returns `true`, 0 tokens arrive.
5. Attacker takes the lender's buy offer: `sellerPos.debt += units`, `isHealthy` returns `true` (phantom collateral covers debt), borrow succeeds.
6. Attacker walks away with loan tokens; position has `debt > 0`, `collateral[0] = 1e18` in storage, `token.balanceOf(midnight) = 0`.

## Impact Explanation
The attacker holds real loan tokens with zero actual collateral backing. The position is immediately insolvent: `token.balanceOf(midnight)` for the collateral is 0, so liquidation seizes nothing. The full debt becomes bad debt, socialised across all lenders in the market via the `lossFactor` mechanism. This is direct theft of lender funds with no collateral at risk.

## Likelihood Explanation
Preconditions require no privileged access: deploying a fee-on-transfer token is trivial, and `touchMarket` is permissionless with no token whitelist. The practical barrier is attracting a victim lender. This can be overcome by seeding initial liquidity to attract other lenders, or by deploying an upgradeable proxy collateral token that activates the fee only after market creation and initial liquidity is deposited. The attack is repeatable across any number of markets.

## Recommendation
1. **Check-effects-interactions**: Move `SafeTransferLib.safeTransferFrom` before any state mutations in `supplyCollateral`, and verify the actual balance received matches `assets` (balance-before/balance-after delta check).
2. **Balance-delta validation**: After the transfer, assert `IERC20(collateralToken).balanceOf(address(this)) - balanceBefore >= assets`.
3. **Token whitelist in `touchMarket`**: Optionally restrict collateral tokens to a governance-approved whitelist to prevent malicious token markets from being created permissionlessly.

## Proof of Concept
```solidity
// 1. Deploy FeeToken: transferFrom always returns true, transfers 0 tokens
// 2. touchMarket with FeeToken as collateral, valid LLTV/maxLif
// 3. Victim lender calls deposit/buy offer with real loan tokens
// 4. Attacker calls supplyCollateral(market, 0, 1e18, attacker)
//    → _position.collateral[0] = 1e18, balanceOf(midnight, FeeToken) = 0
// 5. Attacker calls take on victim's buy offer
//    → isHealthy returns true (phantom collateral), debt assigned to attacker
//    → attacker receives real loan tokens
// 6. Assert: attacker.balance(loanToken) > 0, midnight.balance(FeeToken) == 0
//            position.debt > 0, position.collateral[0] == 1e18 (phantom)
```
A Foundry test deploying a `MockFeeToken` with `transferFrom` returning `true` and transferring 0 tokens, then executing steps 2–6 above, will demonstrate the full exploit path deterministically.

### Citations

**File:** src/Midnight.sol (L476-476)
```text
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

**File:** src/Midnight.sol (L532-545)
```text
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

**File:** src/Midnight.sol (L944-959)
```text
    function isHealthy(Market memory market, bytes32 id, address borrower) public view returns (bool) {
        Position storage _position = position[id][borrower];
        uint256 debt = _position.debt;
        uint256 maxDebt;
        if (debt > 0) {
            uint128 _collateralBitmap = _position.collateralBitmap;
            while (_collateralBitmap != 0) {
                uint256 i = UtilsLib.msb(_collateralBitmap);
                CollateralParams memory collateralParam = market.collateralParams[i];
                uint256 price = IOracle(collateralParam.oracle).price();
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
                _collateralBitmap = _collateralBitmap.clearBit(i);
            }
        }
        return maxDebt >= debt;
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
