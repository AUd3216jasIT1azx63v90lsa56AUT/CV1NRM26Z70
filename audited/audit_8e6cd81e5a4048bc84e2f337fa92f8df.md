Audit Report

## Title
Unbounded Oracle Gas Consumption in `liquidate()` While Loop Enables Permanent Liquidation DoS - (File: src/Midnight.sol)

## Summary
The `liquidate()` function iterates over all of a borrower's activated collaterals in an unbounded while loop, making uncapped external `IOracle.price()` calls for each. Because market creation is permissionless and any contract implementing `IOracle` is accepted without validation, an attacker who acts as both market creator and borrower can deploy gas-exhausting oracle contracts, activate all 16 allowed collaterals, and render their position permanently unliquidatable by exhausting transaction gas before the repay/seize logic executes.

## Finding Description

**Code path — `src/Midnight.sol` lines 607–618:**

```solidity
while (_collateralBitmap != 0) {
    uint256 i = UtilsLib.msb(_collateralBitmap);
    CollateralParams memory _collateralParam = market.collateralParams[i];
    uint256 price = IOracle(_collateralParam.oracle).price(); // uncapped external call
    ...
    _collateralBitmap = _collateralBitmap.clearBit(i);
}
```

The loop runs up to `MAX_COLLATERALS_PER_BORROWER = 16` times (`src/libraries/ConstantsLib.sol` line 21). Each iteration makes an uncapped external call to an oracle address fixed at market creation time and embedded in `CollateralParams`. No gas stipend or `gasleft()` guard is applied.

**Root cause:** No gas limit is imposed on `IOracle.price()` calls. Market creation is permissionless — any contract implementing `IOracle` is accepted with no validation of oracle behavior.

**Exploit flow:**
1. Attacker deploys 16 oracle contracts, each implementing `IOracle.price()` to return a valid price while consuming approximately `block_gas_limit / 17` gas (e.g., via a tight computation loop or storage-heavy operations).
2. Attacker creates a market with these 16 collateral params (permissionless).
3. Attacker calls `supplyCollateral` for all 16 collaterals, activating each in the bitmap. The `TooManyActivatedCollaterals` check at line 539 allows exactly 16 (`src/Midnight.sol` lines 538–540).
4. Attacker borrows, making the position initially healthy.
5. Attacker allows the position to become unhealthy (price movement or post-maturity).
6. Any liquidator calls `liquidate()`. The while loop executes 16 oracle calls, each consuming `~block_gas_limit / 17` gas. Total oracle gas ≈ `16 × (block_gas_limit / 17)` ≈ `0.94 × block_gas_limit`. The transaction runs out of gas before reaching the bad-debt realization and repay/seize logic at lines 626–677.

**Why existing checks fail:**

The LIVENESS section (`src/Midnight.sol` lines 142–158) explicitly acknowledges that oracle *reverts* block liquidation but does not address gas exhaustion — a distinct and more dangerous failure mode that cannot be mitigated by the caller. The `NotLiquidatable` check at line 620 is after the while loop and cannot rescue a gas-exhausted transaction. The `liquidatorGate` check at lines 597–600 is before the loop but is irrelevant to gas exhaustion. No gas cap or `gasleft()` guard exists anywhere in the oracle call path.

## Impact Explanation

The unhealthy position becomes permanently unliquidatable. Bad debt is never realized, violating the core protocol invariant that unhealthy positions remain liquidatable. The borrower retains borrowed assets while lenders bear unrecognized, socialized losses. The attack is irreversible once the position is established: no liquidator can succeed regardless of gas price or block gas limit, because the attacker can binary-search the exact per-oracle gas consumption needed to exhaust any liquidation attempt. This constitutes a permanent freeze of the bad-debt realization mechanism and a direct, concrete financial loss to lenders in the affected market.

## Likelihood Explanation

All preconditions are reachable by an unprivileged user: market creation is permissionless, oracle contracts are attacker-controlled by virtue of being market creator, and activating exactly 16 collaterals is enforced (not prevented) by `MAX_COLLATERALS_PER_BORROWER`. The attacker is the same address acting as market creator and borrower — both are permissionless roles. The gas threshold is tunable: the attacker can precisely calibrate oracle gas consumption to always exceed available gas. The attack is repeatable across multiple markets and positions.

## Recommendation

Apply a gas cap to each `IOracle.price()` call using a fixed stipend (e.g., `IOracle(_collateralParam.oracle).price{gas: ORACLE_GAS_LIMIT}()`), where `ORACLE_GAS_LIMIT` is a protocol-defined constant (e.g., 100,000 gas). If the oracle call runs out of gas or reverts, treat it as a revert (consistent with the existing LIVENESS acknowledgment). Alternatively, require oracle contracts to be whitelisted or validated at market creation time to prevent attacker-controlled gas-exhausting oracles from being registered.

## Proof of Concept

**Minimal Foundry test plan:**

1. Deploy a `MaliciousOracle` contract implementing `IOracle.price()` that spins in a loop consuming approximately `block.gaslimit / 17` gas before returning a valid price.
2. Create a market with 16 `CollateralParams` entries, each pointing to a `MaliciousOracle` instance.
3. As the attacker, call `supplyCollateral` 16 times (one per collateral index) and then `borrow` to establish debt.
4. Advance time past maturity (or manipulate oracle prices to make the position unhealthy).
5. As a separate liquidator address, call `liquidate()` with a sufficient gas limit.
6. Assert the transaction reverts with out-of-gas, confirming the position is permanently unliquidatable.
7. Verify that no gas limit, however large, allows liquidation to succeed by scaling oracle gas consumption proportionally.