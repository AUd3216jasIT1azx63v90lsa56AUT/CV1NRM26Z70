### Title
`liquidate` Lacks Slippage Protection Against Oracle-Manipulation Sandwich Attacks — (File: src/Midnight.sol)

### Summary
The `liquidate` function reads the oracle price on-chain and uses it to compute either `seizedAssets` or `repaidUnits` with no caller-supplied minimum/maximum bound. A malicious borrower can use flash loans to temporarily inflate the oracle price, causing the liquidator to receive fewer seized assets than expected. This makes liquidations unprofitable, discourages them, and allows bad debt to accumulate — which is then socialized among all lenders via `lossFactor`.

### Finding Description

**Root cause — no slippage parameter in `liquidate`:**

The function signature accepts either `seizedAssets` or `repaidUnits` as input, but provides no `minSeizedAssets` / `maxRepaidUnits` guard: [1](#0-0) 

The oracle price is fetched live from an external contract: [2](#0-1) 

That price is then used directly to compute the other side of the trade: [3](#0-2) 

When `repaidUnits > 0`, the liquidator pays a fixed number of units and receives `seizedAssets = repaidUnits * lif / WAD * ORACLE_PRICE_SCALE / liquidatedCollatPrice`. A higher oracle price yields fewer seized assets. There is no floor on `seizedAssets`.

**Exploit path (borrower sandwich):**

1. Borrower's position becomes unhealthy; liquidator broadcasts `liquidate(..., repaidUnits = D, ...)`.
2. Borrower front-runs: takes a flash loan of the collateral token and buys it on the spot market, pushing the oracle price from `P` to `P'` (where `P' < D * ORACLE_PRICE_SCALE * WAD / (C * LLTV)` so the position remains liquidatable).
3. Liquidator's transaction executes at `liquidatedCollatPrice = P'`; `seizedAssets` is proportionally reduced.
4. Borrower back-runs: sells the collateral token, restores the price, repays the flash loan.
5. Net result: borrower retains more collateral; liquidator's profit margin is eroded or eliminated.

The `callback` parameter can be used by sophisticated liquidators to implement their own slippage check, but it is optional and not enforced by the protocol. [4](#0-3) 

### Impact Explanation

**High impact.** Liquidators who do not use a callback contract receive no slippage protection. If liquidations become unprofitable, unhealthy positions go unliquidated, bad debt accumulates, and `lossFactor` is updated to socialize losses across all lenders in the market. [5](#0-4) 

### Likelihood Explanation

**Medium likelihood.** The protocol allows permissionless market creation with any `IOracle` implementation. Spot-price oracles (e.g., Uniswap V2/V3 instantaneous price) are commonly used and are manipulable via flash loans. The SECURITY.md explicitly keeps oracle manipulation/flash-loan attacks **in scope**. The borrower has a direct financial incentive (saving collateral) that can exceed the flash-loan fee on large positions. [6](#0-5) 

### Recommendation

Add explicit slippage bounds to `liquidate`:

```solidity
function liquidate(
    Market calldata market,
    uint256 collateralIndex,
    uint256 seizedAssets,
    uint256 repaidUnits,
    uint256 minSeizedAssets,   // NEW: revert if seizedAssets < this
    uint256 maxRepaidUnits,    // NEW: revert if repaidUnits > this
    address borrower,
    ...
)
```

After computing the final `seizedAssets` / `repaidUnits` from the oracle price, add:

```solidity
require(seizedAssets >= minSeizedAssets, SlippageExceeded());
require(repaidUnits <= maxRepaidUnits, SlippageExceeded());
```

Alternatively, at minimum, document clearly that liquidators **must** use the `callback` mechanism to enforce their own slippage bounds, and provide a reference callback implementation.

### Proof of Concept

```
Setup:
  - Market uses a Uniswap V2 spot-price oracle for collateral token C.
  - Borrower has: debt = 1000 units, collateral = 1500 C tokens, LLTV = 0.8.
  - Oracle price P = 1e36 (1:1). maxDebt = 1500 * 0.8 = 1200 > 1000 → unhealthy.
  - Liquidator submits: liquidate(..., seizedAssets=0, repaidUnits=1000, ...)
    Expected seizedAssets ≈ 1000 * lif * ORACLE_PRICE_SCALE / P = ~1050 C tokens.

Attack:
  1. Borrower sees tx in mempool.
  2. Borrower flash-loans 5000 C tokens, buys them on Uniswap → oracle price rises to P' = 1.1e36.
     New maxDebt = 1500 * 1.1 * 0.8 = 1320 > 1000 → still liquidatable.
  3. Liquidator's tx executes:
     seizedAssets = 1000 * lif * ORACLE_PRICE_SCALE / 1.1e36 ≈ 954 C tokens (vs. expected 1050).
  4. Borrower sells 5000 C tokens back, restores price, repays flash loan.
  5. Borrower saved ~96 C tokens of collateral minus flash-loan fee.
  6. Liquidator received 9% fewer assets than expected; if margin < 9%, liquidation was unprofitable.
``` [7](#0-6) [3](#0-2)

### Citations

**File:** src/Midnight.sol (L581-591)
```text
    function liquidate(
        Market calldata market,
        uint256 collateralIndex,
        uint256 seizedAssets,
        uint256 repaidUnits,
        address borrower,
        bool postMaturityMode,
        address receiver,
        address callback,
        bytes calldata data
    ) external returns (uint256, uint256) {
```

**File:** src/Midnight.sol (L610-611)
```text
            uint256 price = IOracle(_collateralParam.oracle).price();
            if (i == collateralIndex) liquidatedCollatPrice = price;
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/Midnight.sol (L626-641)
```text
        if (badDebt > 0) {
            // forge-lint: disable-next-item(unsafe-typecast) as badDebt <= _position.debt
            _position.debt -= uint128(badDebt);
            uint256 _totalUnits = _marketState.totalUnits;
            uint256 _lossFactor = _marketState.lossFactor;
            _marketState.lossFactor = UtilsLib.toUint128(
                type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
            );
            _marketState.totalUnits -= UtilsLib.toUint128(badDebt);
            _marketState.continuousFeeCredit = _lossFactor < type(uint128).max
                ? UtilsLib.toUint128(
                    _marketState.continuousFeeCredit
                        .mulDivDown(type(uint128).max - _marketState.lossFactor, type(uint128).max - _lossFactor)
                )
                : 0;
        }
```

**File:** src/Midnight.sol (L649-653)
```text
            if (seizedAssets > 0) {
                repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
            } else {
                seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
            }
```

**File:** src/Midnight.sol (L698-715)
```text
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
```

**File:** src/interfaces/IOracle.sol (L5-7)
```text
interface IOracle {
    function price() external view returns (uint256);
}
```
