### Title
Spot Oracle Price Manipulation via Flash Loan Enables Unauthorized Collateral Withdrawal and Unfair Liquidation — (File: src/Midnight.sol)

---

### Summary

The protocol reads `IOracle(collateralParam.oracle).price()` as a raw spot value at the exact moment of each transaction in both `isHealthy()` and `liquidate()`, with no manipulation-resistance requirement. Because market creation is permissionless and any oracle address is accepted, a market can legitimately use a spot-price oracle (e.g., Uniswap V2 instantaneous price, Compound `exchangeRateStored`). An attacker can flash-borrow to transiently inflate or deflate that spot price within a single transaction, pass the health check with a false price, and either withdraw collateral that should be locked or liquidate a position that is genuinely healthy.

---

### Finding Description

**Root cause — unconditional spot-price read with no manipulation guard**

`isHealthy()` iterates every activated collateral and calls the oracle directly: [1](#0-0) 

`liquidate()` performs the identical read to compute `maxDebt` and `liquidatedCollatPrice`: [2](#0-1) 

`withdrawCollateral()` gates the withdrawal on `isHealthy()`: [3](#0-2) 

`take()` gates the seller's final health check on `isHealthy()` after the seller callback has already executed: [4](#0-3) 

The oracle interface imposes no TWAP, no staleness check, and no manipulation-resistance requirement: [5](#0-4) 

Market creation is permissionless and accepts any oracle address per collateral: [6](#0-5) 

**Attack path A — unauthorized collateral withdrawal (price inflation)**

1. Attacker holds a position whose debt is near the health boundary.
2. Attacker flash-borrows a large amount of the collateral token (or the token that drives the oracle's spot price).
3. Attacker buys into the AMM pool (or borrows on Compound) to transiently inflate the oracle's `price()` return value.
4. Attacker calls `withdrawCollateral()`. `isHealthy()` reads the inflated price, computes an artificially high `maxDebt`, and the check passes.
5. Attacker withdraws collateral that should be locked.
6. Attacker repays the flash loan. Oracle price reverts to its true value.
7. The position is now undercollateralized; bad debt is socialized to lenders.

**Attack path B — unfair liquidation of a healthy position (price deflation)**

1. Attacker identifies a healthy victim position.
2. Attacker flash-borrows and dumps the collateral token to transiently deflate the oracle price.
3. Attacker calls `liquidate()`. The deflated price lowers `maxDebt` below `originalDebt`, satisfying `originalDebt > maxDebt`.
4. Attacker seizes collateral at a discount.
5. Attacker repays the flash loan.

**Attack path C — debt increase through seller callback (price inflation during `take`)**

1. Attacker acts as seller in a `take`, increasing their debt.
2. Attacker's seller callback (which executes before the final `isHealthy()` check at line 476) uses an external flash loan to inflate the collateral oracle price.
3. `isHealthy()` passes with the inflated price.
4. After the transaction the oracle reverts; the position is undercollateralized. [7](#0-6) 

---

### Impact Explanation

- **Direct theft / bad debt creation**: Path A lets the attacker extract collateral that backs outstanding debt, leaving lenders with unrecoverable losses socialized via the `lossFactor` mechanism.
- **Unfair seizure**: Path B lets the attacker seize a healthy borrower's collateral at a discount, constituting direct theft from the victim.
- **Protocol insolvency**: Repeated exploitation of Path A across multiple markets can drain collateral reserves, making the protocol insolvent.

Severity: **Critical** — direct, repeatable loss of user funds with no privileged precondition.

---

### Likelihood Explanation

- Market creation is permissionless; any oracle is accepted. Spot-price oracles (Uniswap V2/V3 instantaneous, Compound `exchangeRateStored`) are common and widely deployed.
- Flash loans are freely available on Aave, Balancer, and Uniswap V3 with no credit requirement.
- The attacker needs only enough capital to move the oracle price for one block, which is proportional to pool depth but achievable for many real markets.
- The protocol's own `flashLoan()` function can be composed with the attack in a single multicall. [8](#0-7) 

---

### Recommendation

1. **Require manipulation-resistant oracles**: Document and enforce (via interface or market-creation checks) that oracles must return a TWAP or otherwise manipulation-resistant price (e.g., Uniswap V3 TWAP, Chainlink with staleness check).
2. **Add a price-deviation circuit breaker**: Compare the oracle price against a stored reference; revert if the deviation exceeds a threshold within a single block.
3. **Minimum TWAP window**: Require oracles to expose a `twapWindow()` getter and reject markets whose window is below a protocol-defined minimum (e.g., 30 minutes).

---

### Proof of Concept

```
Setup:
  - Market: loanToken = USDC, collateral = WETH, oracle = UniswapV2SpotOracle(WETH/USDC pool)
  - Attacker position: 1 ETH collateral, 900 USDC debt (LLTV = 0.915, maxDebt ≈ 915 USDC → healthy)

Attack (single transaction):
  1. flashLoan(WETH, 10_000e18) from Aave
  2. Swap 10_000 WETH → USDC in the Uniswap V2 pool
     → WETH spot price inflates from $1,000 to ~$5,000
  3. Call withdrawCollateral(market, 0, 0.8e18, attacker, attacker)
     isHealthy(): maxDebt = 0.2 ETH * $5,000 * 0.915 = $915 >= $900 debt → PASSES
     0.8 ETH is transferred to attacker
  4. Swap USDC back to WETH (or repay Aave flash loan directly)
  5. Oracle price reverts to $1,000
     Remaining position: 0.2 ETH collateral, 900 USDC debt
     maxDebt = 0.2 * $1,000 * 0.915 = $183 << $900 → severely undercollateralized
     Bad debt socialized to lenders via lossFactor

Expected outcome: Attacker extracts 0.8 ETH (~$800) at the cost of gas only;
lenders absorb ~$717 in bad debt.
```

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

**File:** src/Midnight.sol (L568-568)
```text
        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());
```

**File:** src/Midnight.sol (L607-618)
```text
        while (_collateralBitmap != 0) {
            uint256 i = UtilsLib.msb(_collateralBitmap);
            CollateralParams memory _collateralParam = market.collateralParams[i];
            uint256 price = IOracle(_collateralParam.oracle).price();
            if (i == collateralIndex) liquidatedCollatPrice = price;
            uint256 _collateral = _position.collateral[i];
            maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(_collateralParam.lltv, WAD);
            badDebt = badDebt.zeroFloorSub(
                _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
            );
            _collateralBitmap = _collateralBitmap.clearBit(i);
        }
```

**File:** src/Midnight.sol (L737-752)
```text
    function flashLoan(address[] calldata tokens, uint256[] calldata assets, address callback, bytes calldata data)
        external
    {
        require(tokens.length == assets.length, InconsistentInput());
        emit EventsLib.FlashLoan(msg.sender, tokens, assets, callback);
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
    }
```

**File:** src/Midnight.sol (L950-957)
```text
            while (_collateralBitmap != 0) {
                uint256 i = UtilsLib.msb(_collateralBitmap);
                CollateralParams memory collateralParam = market.collateralParams[i];
                uint256 price = IOracle(collateralParam.oracle).price();
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
                _collateralBitmap = _collateralBitmap.clearBit(i);
            }
```

**File:** src/interfaces/IOracle.sol (L1-7)
```text
// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (c) 2025 Morpho Association
pragma solidity >=0.5.0;

interface IOracle {
    function price() external view returns (uint256);
}
```

**File:** src/interfaces/IMidnight.sol (L14-19)
```text
struct CollateralParams {
    address token;
    uint256 lltv;
    uint256 maxLif;
    address oracle;
}
```
