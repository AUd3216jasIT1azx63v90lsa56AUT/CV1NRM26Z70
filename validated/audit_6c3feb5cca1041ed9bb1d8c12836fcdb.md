### Title
Dust Borrower Positions Can Be Created With No Minimum Size Enforcement, Making Liquidation Unprofitable and Causing Permanent Bad Debt — (`src/Midnight.sol`)

### Summary

Morpho Midnight enforces no minimum debt or collateral size on borrower positions. A borrower can open a normal-sized position, repay nearly all debt via `repay()`, and withdraw nearly all collateral via `withdrawCollateral()`, leaving a "dust" position with negligible collateral and debt. When such a position becomes unhealthy, the liquidation incentive (LIF bonus on dust collateral) is worth far less than the gas cost of the `liquidate()` call. Liquidators rationally skip these positions, and the corresponding debt is never repaid or realized as bad debt, permanently stranding lender credit.

### Finding Description

**Root cause — no minimum position size after partial exit:**

`repay()` subtracts any nonzero `units` from `position[id][onBehalf].debt` with no floor check on the remaining balance:

```solidity
// src/Midnight.sol:508
position[id][onBehalf].debt -= UtilsLib.toUint128(units);
```

`withdrawCollateral()` only requires the position to remain healthy after withdrawal:

```solidity
// src/Midnight.sol:568
require(isHealthy(market, id, onBehalf), UnhealthyBorrower());
```

Neither function enforces a minimum remaining debt or collateral amount. There is no analogue to a `MIN_NET_COLL` or `MIN_DEBT` constant anywhere in the codebase.

**Attack / accidental path:**

1. Borrower supplies collateral `C` and borrows `D` units via `take()` (sell offer).
2. Borrower calls `repay()` leaving `ε` debt (e.g. 1–100 wei of loan token units).
3. Borrower calls `withdrawCollateral()` leaving `δ` collateral — just enough so `δ * price / ORACLE_PRICE_SCALE * lltv / WAD >= ε` (position is healthy).
4. The position now has dust-level debt `ε` and dust-level collateral `δ`.
5. Oracle price drops by any small amount → `maxDebt < debt` → position is unhealthy.
6. Liquidation profit = `δ * LIF - gas_cost`. For dust `δ`, this is negative at any realistic gas price.
7. No liquidator calls `liquidate()`. The debt `ε` is never repaid and never realized as bad debt.
8. `marketState[id].withdrawable` never increases by `ε`, so lenders holding the corresponding credit units cannot withdraw them.

**Post-maturity amplification:**

After `block.timestamp > market.maturity`, every position with nonzero debt is liquidatable regardless of health. The LIF ramps from 1 (no incentive) to `maxLif` over `TIME_TO_MAX_LIF = 15 minutes`. Dust positions at LIF = 1 yield zero profit for the liquidator. Even at `maxLif`, the absolute incentive on dust collateral is negligible. These positions will never be liquidated, permanently blocking the market from fully unwinding.

**Why `rcfThreshold` does not fix this:**

The `rcfThreshold` parameter deactivates the Recovery Close Factor for small residual collateral, allowing full liquidation of a position in one call. However, it does not make the liquidation *profitable* — it only removes the partial-liquidation constraint. If the entire position is dust, full liquidation still yields less value than gas cost, so liquidators still skip it.

### Impact Explanation

- Dust positions that become unhealthy are never liquidated. The debt `ε` is never repaid and never socialized via `lossFactor`. Lenders holding the corresponding credit units cannot withdraw them.
- In a market with many dust positions (created deliberately or organically through partial repayments), the aggregate stuck credit can be material.
- Post-maturity, the market cannot fully unwind: `withdrawable` will always be less than `totalUnits` by the sum of all dust debts, permanently locking a portion of lender funds.
- This is a direct, permanent loss of lender funds with no recovery path once the market matures and the dust positions are confirmed unprofitable to liquidate.

### Likelihood Explanation

- Any borrower can create a dust position with two standard transactions (`repay` + `withdrawCollateral`) at negligible cost (only the dust amounts are sacrificed).
- No privileged access is required.
- It can happen accidentally (borrowers partially exiting) or deliberately (griefing lenders).
- Markets with high-LLTV collateral (e.g. `LLTV_8 = 1e18`) are especially vulnerable because `maxLif = 1` (no liquidation incentive at all), making even non-dust positions unprofitable to liquidate.

### Recommendation

1. **Enforce a minimum debt floor**: In `repay()`, after subtracting units, require that the remaining debt is either zero or above a protocol-defined `MIN_DEBT` threshold (e.g. expressed in loan token units, set per market or globally).
2. **Enforce a minimum collateral floor**: In `withdrawCollateral()`, after the health check, require that if debt > 0, the remaining collateral value (in loan token units) is above a `MIN_COLLATERAL_VALUE` threshold.
3. **Alternatively, enforce a minimum at borrow time**: In `take()` for sell offers (debt-increasing path), require that the resulting debt is above `MIN_DEBT`. This prevents dust positions from being created in the first place.
4. Consider making `rcfThreshold` a hard minimum: if the entire position's collateral value is below `rcfThreshold`, allow liquidation to proceed even at a loss (subsidized by the protocol or via a gas stipend mechanism).

### Proof of Concept

```solidity
// Pseudocode - Foundry test sketch
function testDustPosition() public {
    // 1. Borrower opens a normal position: supply 1e18 collateral, borrow 1000 units
    midnight.supplyCollateral(market, 0, 1e18, borrower);
    midnight.take(sellOffer, ratifierData, 1000e18, borrower, ...); // borrow 1000 units

    // 2. Repay all but 1 wei of debt
    midnight.repay(market, 1000e18 - 1, borrower, address(0), "");
    // position.debt == 1

    // 3. Withdraw all but dust collateral (just enough to stay healthy)
    // maxDebt = dustColl * price / 1e36 * lltv / 1e18 >= 1
    // dustColl = ceil(1e36 * 1e18 / (price * lltv))
    uint256 dustColl = ...; // computed from price and lltv
    midnight.withdrawCollateral(market, 0, 1e18 - dustColl, borrower, borrower);
    // position.collateral[0] == dustColl, position.debt == 1

    // 4. Oracle price drops 1 wei → position is unhealthy
    oracle.setPrice(oracle.price() - 1);
    assert(!midnight.isHealthy(market, id, borrower));

    // 5. Liquidation profit = dustColl * LIF / oracle_price - gas_cost < 0
    // Liquidator skips. Debt never repaid. Lender credit stuck.
    // marketState.withdrawable never increases by 1 unit.
    // Lender holding 1 credit unit cannot withdraw it.
}
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

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

**File:** src/Midnight.sol (L549-573)
```text
    function withdrawCollateral(
        Market memory market,
        uint256 collateralIndex,
        uint256 assets,
        address onBehalf,
        address receiver
    ) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        address collateralToken = market.collateralParams[collateralIndex].token;

        Position storage _position = position[id][onBehalf];
        uint256 newCollateral = _position.collateral[collateralIndex] - assets;
        _position.collateral[collateralIndex] = UtilsLib.toUint128(newCollateral);

        if (newCollateral == 0 && assets > 0) {
            _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
        }

        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());

        emit EventsLib.WithdrawCollateral(msg.sender, id, collateralToken, assets, onBehalf, receiver);

        SafeTransferLib.safeTransfer(collateralToken, receiver, assets);
    }
```

**File:** src/Midnight.sol (L655-668)
```text
            if (!postMaturityMode) {
                uint256 lltv = market.collateralParams[collateralIndex].lltv;
                // Note that debt >= maxDebt in this branch.
                // The imprecision in this computation is at most a few hundreds collateral or loan token assets.
                uint256 maxRepaid = lltv < WAD
                    ? (_position.debt - maxDebt).mulDivUp(WAD * WAD, WAD * WAD - lif * lltv)
                    : type(uint256).max;
                require(
                    repaidUnits <= maxRepaid
                        || _position.collateral[collateralIndex].mulDivDown(liquidatedCollatPrice, ORACLE_PRICE_SCALE)
                            .mulDivDown(WAD, lif).zeroFloorSub(maxRepaid) < market.rcfThreshold,
                    RecoveryCloseFactorConditionsViolated()
                );
            }
```

**File:** src/libraries/ConstantsLib.sol (L19-19)
```text
uint256 constant TIME_TO_MAX_LIF = 15 minutes;
```
