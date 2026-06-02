Audit Report

## Title
Missing oracle address validation in `touchMarket` enables permanent liquidation DoS - (File: src/Midnight.sol)

## Summary
`touchMarket` validates collateral token address, LLTV tier, and `maxLif` but imposes no constraint on `collateralParams[i].oracle`. Any unprivileged market creator can deploy a market with `oracle = address(0)` for any collateral slot. Once a borrower activates that slot, every subsequent call to `liquidate` unconditionally invokes `IOracle(address(0)).price()` inside the bitmap loop, which reverts because `address(0)` has no code and returns empty data that cannot be ABI-decoded as `uint256`. The position becomes permanently unliquidatable, freezing lender funds and preventing bad-debt realization.

## Finding Description
**Root cause — `touchMarket` validation loop (lines 762–772):**

The loop enforces `collateralToken > previousCollateralToken`, `isLltvAllowed(lltv)`, and a valid `maxLif`, but contains no check on the oracle field:

```solidity
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

There is no `require(market.collateralParams[i].oracle != address(0))`.

**Trigger — `liquidate` bitmap loop (lines 607–618):**

```solidity
while (_collateralBitmap != 0) {
    uint256 i = UtilsLib.msb(_collateralBitmap);
    CollateralParams memory _collateralParam = market.collateralParams[i];
    uint256 price = IOracle(_collateralParam.oracle).price(); // line 610
    ...
}
```

The call at line 610 is unconditional. If `_collateralParam.oracle == address(0)`, the EVM CALL to `address(0)` returns 0 bytes; Solidity 0.8's ABI decoder for `uint256` reverts on empty returndata.

**Market ID locks in the zero oracle:**

The market ID is derived from a full hash of the market struct including every oracle field. A liquidator must pass the exact market struct used at creation time; they cannot substitute a valid oracle address without producing a different (uncreated) market ID.

**Certora invariant gap:**

The `createdMarketsHaveNonZeroCollaterals` invariant only proves `token != 0`, not `oracle != 0`:

```
strong invariant createdMarketsHaveNonZeroCollaterals(Midnight.Market market, uint256 i)
    marketIsCreated(market) => i < market.collateralParams.length => market.collateralParams[i].token != 0;
```

The `oracleRevertCausesLiquidateRevert` rule in `Reverts.spec` explicitly proves the revert propagation — confirming the behavior, not preventing it.

**Exploit flow:**
1. Attacker calls `touchMarket` with `collateralParams[i].oracle = address(0)` — passes all validation.
2. Borrower supplies collateral at index `i` and takes a loan; bit `i` is set in `collateralBitmap`.
3. Position becomes unhealthy (price drop or post-maturity).
4. Any liquidator calls `liquidate` with the correct market struct.
5. The loop reaches bit `i`, executes `IOracle(address(0)).price()`.
6. `address(0)` has no code; CALL returns 0 bytes; ABI decoder reverts.
7. `liquidate` always reverts; the unhealthy position can never be liquidated.

## Impact Explanation
Lenders' funds are frozen. The unhealthy position accumulates bad debt that can never be realized through `liquidate`. The core invariant "unhealthy positions must remain liquidatable" is permanently violated for any market created with a zero oracle. This constitutes a permanent fund freeze and liquidation bypass — both listed as high-severity impact classes in `RESEARCHER.md` ("Permanent lock, freeze, or unrecoverable corruption of user/project state").

## Likelihood Explanation
Market creation is fully permissionless — no admin approval is required. The attacker only needs to call `touchMarket` with a crafted `CollateralParams` array containing `oracle = address(0)`. The condition is trivially reachable in a single transaction. The DoS is permanent and repeatable across any number of markets.

## Recommendation
Add a non-zero oracle check inside the `touchMarket` validation loop, immediately after or alongside the existing collateral token check:

```solidity
require(market.collateralParams[i].oracle != address(0), OracleIsZero());
```

Additionally, extend the `createdMarketsHaveNonZeroCollaterals` Certora invariant (or add a new one) to assert `market.collateralParams[i].oracle != 0` for all created markets, closing the formal verification gap.

## Proof of Concept
1. Deploy a minimal `IOracle`-compatible contract for a valid collateral token (needed only to pass the `token > previousToken` sort check; the oracle for that slot can be valid).
2. Call `touchMarket` with a single `CollateralParams` entry: `token = <any valid ERC-20>`, `lltv = <allowed tier>`, `maxLif = maxLif(lltv, LIQUIDATION_CURSOR_LOW)`, **`oracle = address(0)`**.
3. As a borrower, call `supplyCollateral` at index 0 and then `take` to open a debt position.
4. Advance time past `market.maturity` (or manipulate price to make position unhealthy).
5. Call `liquidate` with `postMaturityMode = true` and the exact market struct from step 2.
6. Observe unconditional revert at `IOracle(address(0)).price()` — position is permanently unliquidatable.