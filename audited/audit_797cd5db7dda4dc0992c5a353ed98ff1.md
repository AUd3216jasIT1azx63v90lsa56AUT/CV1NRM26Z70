Audit Report

## Title
Fee-on-Transfer Collateral Inflates `position.collateral` Accounting, Enabling Undercollateralized Borrowing - (File: src/Midnight.sol)

## Summary
`supplyCollateral` writes the caller-supplied `assets` value into `_position.collateral[collateralIndex]` at line 533 before executing the ERC20 transfer at line 545, with no balance-delta verification. For fee-on-transfer collateral tokens, the protocol records more collateral than it actually receives. The subsequent `isHealthy` check uses the inflated storage value, permitting undercollateralized debt that cannot be fully recovered at liquidation, creating bad debt socialized among lenders.

## Finding Description
**Root cause — accounting write precedes transfer, no delta check:**

In `supplyCollateral` (`src/Midnight.sol:533`), the storage update unconditionally trusts `assets`:
```solidity
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets);
```
The actual ERC20 transfer occurs at line 545:
```solidity
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
```
`SafeTransferLib.safeTransferFrom` (`src/libraries/SafeTransferLib.sol:24-34`) only checks the boolean return value via `returndata` decoding; it does not snapshot `balanceOf(address(this))` before and after to verify the received delta. For a token with a 1% transfer fee, the protocol receives `assets * 0.99` but records `assets`.

**Health check uses inflated value:**

`isHealthy` (`src/Midnight.sol:954-955`) computes `maxDebt` directly from `_position.collateral[i]`:
```solidity
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
    .mulDivDown(collateralParam.lltv, WAD);
```
No cross-check against actual contract token balance is performed.

**Exploit path via `take` → `onSell` → `supplyCollateral`:**

1. Attacker deploys a fee-on-transfer ERC20 (e.g., 1% fee) and calls `touchMarket` to create a market with it as collateral. `touchMarket` (`src/Midnight.sol:755-791`) validates only LLTV tiers, sorted addresses, and `maxLif` — no token-type restriction.
2. A lender creates a buy offer ratified via `SetterRatifier`.
3. Attacker deploys a callback contract implementing `ISellCallback.onSell` that calls `supplyCollateral(market, index, amount, attacker)`. Attacker calls `setIsAuthorized(callbackContract, true, attacker)` — a standard self-authorization.
4. Attacker calls `take(lenderOffer, ...)` with `sellerCallback = callbackContract`. Inside `take`:
   - `sellerPos.debt += sellerDebtIncrease` is written (`src/Midnight.sol:414`).
   - Loan tokens are transferred to the attacker (`src/Midnight.sol:455-456`).
   - `onSell` is invoked (`src/Midnight.sol:458-473`); inside it, `supplyCollateral` records `amount` but only receives `amount * 0.99`.
   - After the callback, `isHealthy` is called (`src/Midnight.sol:476`) using the inflated `_position.collateral[index] = amount`.
5. Health check passes. The attacker holds debt backed by only `amount * 0.99` actual collateral tokens while the protocol believes it holds `amount`.

**Existing guards are insufficient:**
- `supplyCollateral` has no balance-before/after guard.
- `SafeTransferLib` only checks the return boolean, not the received delta.
- `isHealthy` reads storage directly without comparing to actual balances.
- `touchMarket` imposes no token-type constraints.

## Impact Explanation
The protocol's core solvency invariant is violated immediately upon the first `supplyCollateral` call with a fee-on-transfer token. At liquidation, the seized collateral is less than the recorded amount, leaving unpayable bad debt that is proportionally socialized among lenders. This constitutes direct loss of lender funds and protocol insolvency.

## Likelihood Explanation
Market creation is fully permissionless; any unprivileged user can deploy a fee-on-transfer ERC20 and create a market with it as collateral. The attacker only needs to authorize their own callback contract, which is a standard self-authorization. `SECURITY.md` contains no fee-on-transfer exclusion. The exploit is repeatable on every `supplyCollateral` call with a fee-bearing token and requires no privileged access.

## Recommendation
In `supplyCollateral`, snapshot `IERC20(collateralToken).balanceOf(address(this))` before and after the `safeTransferFrom` call, and use the actual delta — not the caller-supplied `assets` — as the amount to credit to `_position.collateral[collateralIndex]`. This pattern eliminates the discrepancy for fee-on-transfer tokens without requiring any token-type allowlist.

## Proof of Concept
1. Deploy a mock ERC20 with a 1% transfer fee on every `transferFrom`.
2. Call `touchMarket` with this token as the sole collateral param (valid LLTV, valid `maxLif`).
3. Deploy a callback contract that, inside `onSell`, calls `supplyCollateral(market, 0, 1000e18, attacker)`.
4. Call `setIsAuthorized(callbackContract, true, attacker)`.
5. Have a lender place a buy offer; call `take` with `sellerCallback = callbackContract` and `sellerDebtIncrease > 0`.
6. Assert: `position[id][attacker].collateral[0] == 1000e18` but `IERC20(feeToken).balanceOf(address(midnight)) == 990e18`.
7. Assert: `isHealthy` returns `true` despite the 10e18 shortfall.
8. Attempt liquidation at max debt; confirm residual bad debt remains after all collateral is seized. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/Midnight.sol (L458-476)
```text
        if (sellerCallback != address(0)) {
            bytes memory sellerCallbackData = offer.buy ? takerCallbackData : offer.callbackData;
            require(
                ISellCallback(sellerCallback)
                    .onSell(
                        id,
                        offer.market,
                        sellerAssets,
                        units,
                        sellerPendingFeeDecrease,
                        seller,
                        receiver,
                        sellerCallbackData
                    ) == CALLBACK_SUCCESS,
                WrongSellCallbackReturnValue()
            );
        }
        if (!wasLocked) UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, false);
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

**File:** src/Midnight.sol (L531-545)
```text
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

**File:** src/Midnight.sol (L755-773)
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
```

**File:** src/Midnight.sol (L953-956)
```text
                uint256 price = IOracle(collateralParam.oracle).price();
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
                _collateralBitmap = _collateralBitmap.clearBit(i);
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
