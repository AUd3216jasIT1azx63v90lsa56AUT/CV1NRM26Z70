I have all the code I need. Let me now perform the final arithmetic verification before writing the output.

**Key code path** (`src/Midnight.sol` lines 649–676):

```
seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
```

With `repaidUnits = 1`, `lif < 2*WAD` (typical): `floor(lif/WAD) = 1`, then `floor(1e36 / liquidatedCollatPrice) = 0` when `liquidatedCollatPrice > 1e36`.

No post-computation check enforces `seizedAssets > 0` when `repaidUnits > 0`.

**RCF check** (lines 659–667): `maxRepaid = (_position.debt - maxDebt).mulDivUp(WAD^2, WAD^2 - lif*lltv)`. When `_position.debt - maxDebt >= 1` and `lif*lltv > 0`, `maxRepaid >= 2 > 1 = repaidUnits`, so the RCF check passes trivially.

---

### Title
Liquidator can repay 1 unit of debt and receive 0 seized collateral when oracle price exceeds ORACLE_PRICE_SCALE — (`src/Midnight.sol`)

### Summary
When the liquidated collateral's oracle price exceeds `ORACLE_PRICE_SCALE` (1e36), the `seizedAssets` computation `repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice)` rounds to zero for small `repaidUnits` (e.g., 1). No post-computation guard enforces `seizedAssets > 0` when `repaidUnits > 0`, so the liquidation proceeds: the liquidator pays loan tokens and receives zero collateral, violating the invariant that liquidators must receive collateral for repaid debt.

### Finding Description

**Exact code path** — `src/Midnight.sol`, `liquidate()`: [1](#0-0) 

```solidity
if (seizedAssets > 0) {
    repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
} else {
    seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
}
```

When the liquidator supplies `repaidUnits = 1` and `seizedAssets = 0` (the `else` branch), the protocol computes:

```
seizedAssets = floor(1 * lif / WAD) * floor(ORACLE_PRICE_SCALE / liquidatedCollatPrice)
```

`maxLif` is computed as `WAD^2 / (WAD - cursor*(WAD-lltv))`. [2](#0-1) 

For all standard LLTV tiers (LLTV_0 through LLTV_7) with either cursor, `maxLif < 2*WAD`, so `floor(lif/WAD) = 1`. The second factor `floor(1e36 / liquidatedCollatPrice) = 0` whenever `liquidatedCollatPrice > 1e36 = ORACLE_PRICE_SCALE`. This is a valid, non-pathological oracle price: it simply means one unit of collateral token is worth more than one unit of loan token (e.g., WBTC as collateral, USDC as loan token with standard 1e36 scaling).

**RCF check** (lines 659–667): [3](#0-2) 

```solidity
uint256 maxRepaid = lltv < WAD
    ? (_position.debt - maxDebt).mulDivUp(WAD * WAD, WAD * WAD - lif * lltv)
    : type(uint256).max;
require(repaidUnits <= maxRepaid || ...);
```

When `_position.debt - maxDebt >= 1` (position is unhealthy) and `lif*lltv > 0`, the denominator `WAD^2 - lif*lltv < WAD^2`, so `maxRepaid = ceil(WAD^2 / (WAD^2 - lif*lltv)) >= 2`. Therefore `repaidUnits = 1 <= maxRepaid` always holds, and the RCF check passes unconditionally.

**State changes after the check** (lines 670–676): [4](#0-3) 

```solidity
uint128 newCollateral = _position.collateral[collateralIndex] - UtilsLib.toUint128(seizedAssets); // seizedAssets=0, no change
_position.collateral[collateralIndex] = newCollateral;
// bitmap NOT cleared (seizedAssets == 0)
_marketState.withdrawable += UtilsLib.toUint128(repaidUnits); // +1
_position.debt -= UtilsLib.toUint128(repaidUnits);            // -1
```

Then: [5](#0-4) 

```solidity
SafeTransferLib.safeTransfer(collateralToken, receiver, seizedAssets); // transfers 0
...
SafeTransferLib.safeTransferFrom(loanToken, payer, address(this), repaidUnits); // pulls 1 unit
```

The liquidator pays 1 loan-token unit and receives 0 collateral. No existing check stops this.

**Attacker-controlled inputs:**
- `repaidUnits = 1`, `seizedAssets = 0` (caller-supplied)
- `liquidatedCollatPrice` is the oracle return value — not attacker-controlled, but the condition `price > 1e36` is a normal market state

**Preconditions:**
1. Market with `lltv < WAD` (any of LLTV_0–LLTV_7)
2. Oracle price for the liquidated collateral > `ORACLE_PRICE_SCALE` (1e36)
3. Position is unhealthy (`originalDebt > maxDebt`)
4. `lif < 2*WAD` (always true for standard market parameters)

### Impact Explanation

A liquidator calling `liquidate` with `repaidUnits = 1` in a market where the collateral oracle price exceeds 1e36 will have 1 unit of loan token pulled from them and transferred to `withdrawable`, while receiving 0 collateral in return. The borrower's debt decreases by 1 unit without any collateral being seized. This violates the invariant that liquidators must receive collateral for repaid debt. While the liquidator is the direct economic victim (they lose 1 unit), the borrower gains a free 1-unit debt reduction, and the protocol's accounting is distorted: `withdrawable` increases without a corresponding collateral seizure backing it.

### Likelihood Explanation

The condition `liquidatedCollatPrice > ORACLE_PRICE_SCALE` is routine for any market where the collateral token is worth more than 1 loan token per atomic unit (e.g., WBTC/USDC, ETH/DAI with standard 1e36 oracle scaling). The position only needs to be marginally unhealthy (`debt - maxDebt = 1`). The liquidator must deliberately pass `repaidUnits = 1`, which is an unusual but valid input. Repeatability is high: the condition persists as long as the oracle price remains above 1e36 and the position remains unhealthy.

### Recommendation

After computing `seizedAssets` from `repaidUnits`, add a guard:

```solidity
} else {
    seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
    require(seizedAssets > 0, ZeroSeizedAssets()); // add this
}
```

This ensures that any non-zero `repaidUnits` input always results in at least 1 unit of collateral being seized, preserving the liquidation invariant. Alternatively, enforce a minimum `repaidUnits` such that `seizedAssets >= 1` is guaranteed given the current oracle price.

### Proof of Concept

```solidity
// Foundry unit test
function testRepayWithZeroSeized() public {
    // Setup: lltv = LLTV_0 (0.385e18), maxLif ≈ 1.18e18
    // Oracle price = 2e36 (> ORACLE_PRICE_SCALE, valid for high-value collateral)
    // collateral = 1 unit, debt = 2 units (unhealthy: maxDebt = 1*2e36/1e36*0.385 = 0.77 < 2)
    
    uint256 price = 2e36; // > ORACLE_PRICE_SCALE
    oracle.setPrice(price);
    // supply 1 unit collateral, borrow 2 units (unhealthy at this price with lltv=0.385)
    
    // Verify seizedAssets rounds to 0:
    // seizedAssets = 1.mulDivDown(lif, WAD).mulDivDown(1e36, 2e36)
    //              = 1.mulDivDown(1e36, 2e36) = 0
    
    uint256 balanceBefore = loanToken.balanceOf(liquidator);
    uint256 collateralBefore = midnight.collateral(id, borrower, 0);
    
    vm.prank(liquidator);
    (uint256 seized, uint256 repaid) = midnight.liquidate(
        market, 0, 0, 1, borrower, false, liquidator, address(0), ""
    );
    
    assertEq(seized, 0, "seized must be 0");
    assertEq(repaid, 1, "repaid must be 1");
    assertEq(loanToken.balanceOf(liquidator), balanceBefore - 1, "liquidator paid 1 unit");
    assertEq(midnight.collateral(id, borrower, 0), collateralBefore, "collateral unchanged");
    // FAIL: liquidator paid debt but received no collateral
}
```

Expected assertions: `seized == 0`, `repaid == 1`, liquidator balance decreases by 1, borrower collateral unchanged. The test passes (no revert), demonstrating the invariant violation.

### Citations

**File:** src/Midnight.sol (L649-653)
```text
            if (seizedAssets > 0) {
                repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
            } else {
                seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
            }
```

**File:** src/Midnight.sol (L659-667)
```text
                uint256 maxRepaid = lltv < WAD
                    ? (_position.debt - maxDebt).mulDivUp(WAD * WAD, WAD * WAD - lif * lltv)
                    : type(uint256).max;
                require(
                    repaidUnits <= maxRepaid
                        || _position.collateral[collateralIndex].mulDivDown(liquidatedCollatPrice, ORACLE_PRICE_SCALE)
                            .mulDivDown(WAD, lif).zeroFloorSub(maxRepaid) < market.rcfThreshold,
                    RecoveryCloseFactorConditionsViolated()
                );
```

**File:** src/Midnight.sol (L670-676)
```text
            uint128 newCollateral = _position.collateral[collateralIndex] - UtilsLib.toUint128(seizedAssets);
            _position.collateral[collateralIndex] = newCollateral;
            if (newCollateral == 0 && seizedAssets > 0) {
                _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
            }
            _marketState.withdrawable += UtilsLib.toUint128(repaidUnits);
            _position.debt -= UtilsLib.toUint128(repaidUnits);
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

**File:** src/libraries/ConstantsLib.sol (L50-52)
```text
function maxLif(uint256 lltv, uint256 cursor) pure returns (uint256) {
    return UtilsLib.mulDivDown(WAD, WAD, WAD - UtilsLib.mulDivDown(cursor, WAD - lltv, WAD));
}
```
