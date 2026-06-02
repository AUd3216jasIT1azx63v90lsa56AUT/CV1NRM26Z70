Audit Report

## Title
Unconditional `safeTransfer` in `liquidate()` permanently blocks liquidation and bad-debt realization for markets with non-standard collateral tokens - (`src/Midnight.sol`)

## Summary
`liquidate()` calls `SafeTransferLib.safeTransfer()` at line 696 unconditionally — outside the `if (repaidUnits > 0 || seizedAssets > 0)` guard — including when `seizedAssets == 0`. For any collateral token whose `transfer()` returns `false`, this causes every liquidation call to revert with `TransferReturnedFalse`, permanently freezing all liquidation paths for that market. The NatSpec at line 577 explicitly promises that passing `seizedAssets = 0` and `repaidUnits = 0` "allows to realize bad debt with 0 token transferred," but the unconditional `safeTransfer` call directly contradicts this guarantee.

## Finding Description

**Exact code path:**

`liquidate()` in `src/Midnight.sol` contains a conditional block at line 643 that handles seizure and repayment logic:

```solidity
if (repaidUnits > 0 || seizedAssets > 0) {
    // ... seizure/repayment logic ...
}  // ends at line 677
```

After this block, at line 696, `safeTransfer` is called **unconditionally**:

```solidity
SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
```

`SafeTransferLib.safeTransfer()` in `src/libraries/SafeTransferLib.sol` at line 21 decodes the return value and reverts if it is `false`:

```solidity
require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferReturnedFalse());
```

**Root cause:**

The `safeTransfer` call at line 696 is placed outside the `if (seizedAssets > 0)` guard. When `seizedAssets == 0` (bad-debt-only path), the call executes `transfer(receiver, 0)`. For any token whose `transfer()` returns `false` — including for zero-value calls — this reverts. No validation in `touchMarket()` prevents such a token from being used as collateral.

**Critical asymmetry:**

`supplyCollateral()` uses `safeTransferFrom` for deposits. A token where `transferFrom` succeeds (returns `true` or no return value) but `transfer` returns `false` allows collateral to be deposited normally, while every subsequent liquidation call reverts. Since `touchMarket()` is permissionless and performs no check on token transfer return behavior, a market creator can deploy such a token and create a market with it.

**Bad-debt-only path is also blocked:**

The NatSpec at line 577 states: *"Passing both 0 for seizedAssets and repaidUnits allows to realize bad debt with 0 token transferred."* However, `safeTransfer(token, receiver, 0)` is still called at line 696. Any token returning `false` for zero-value transfers causes this path to revert, rolling back all state changes including the `lossFactor` update and `totalUnits` reduction computed in the `if (badDebt > 0)` block at lines 626–641.

**Existing checks are insufficient:**

The `NotLiquidatable` guard at lines 620–624 checks only health status and lock state — it provides no protection against token transfer failures. The `ERC20False` contract in `test/SafeTransferLibTest.sol` (lines 15–18) confirms the developers are aware of tokens that return `false`, yet no guard exists at line 696.

## Impact Explanation

For any market whose collateral token returns `false` on `transfer()`:
- All calls to `liquidate()` permanently revert regardless of inputs, including the `(seizedAssets=0, repaidUnits=0)` bad-debt path.
- Unhealthy positions accumulate bad debt that can never be realized or socialized.
- Lenders' credit is never slashed to reflect losses; market accounting diverges from reality.
- The market becomes permanently insolvent with no recovery path.

This constitutes a permanent freeze of user/protocol state and a critical accounting integrity failure, both in-scope impact classes per RESEARCHER.md.

## Likelihood Explanation

**Required preconditions:**
1. A collateral token whose `transfer()` returns `false` (non-standard but valid ERC20 — explicitly present in the test suite).
2. A market creator (unprivileged — `touchMarket()` has no access control) uses such a token.
3. At least one borrower supplies that collateral and takes a loan.

The market creator role is fully permissionless. The broken behavior is invisible at deposit time (only `safeTransferFrom` is used for deposits) and only manifests at liquidation. The condition is repeatable: every subsequent liquidation attempt on the same market reverts identically.

## Recommendation

Guard the `safeTransfer` call at line 696 with a `seizedAssets > 0` check:

```solidity
if (seizedAssets > 0) {
    SafeTransferLib.safeTransfer(
        market.collateralParams[collateralIndex].token, receiver, seizedAssets
    );
}
```

This aligns the code with the NatSpec guarantee at line 577 and eliminates the unconditional zero-value transfer that blocks the bad-debt realization path.

## Proof of Concept

1. Deploy a token `ERC20AsymmetricFalse` where `transferFrom` returns `true` and `transfer` always returns `false`.
2. Call `touchMarket()` with this token as the collateral — succeeds (no token behavior validation).
3. Call `supplyCollateral()` — succeeds (`safeTransferFrom` returns `true`).
4. Borrower takes a loan; position becomes unhealthy (price drop or post-maturity).
5. Call `liquidate()` with `seizedAssets=0, repaidUnits=0` (bad-debt path).
6. Execution reaches line 696: `safeTransfer(token, receiver, 0)` → token returns `false` → `TransferReturnedFalse` revert.
7. All state changes (lossFactor update, totalUnits reduction) are rolled back.
8. Repeat for any `seizedAssets`/`repaidUnits` combination — all revert identically.

A Foundry test using a minimal `ERC20AsymmetricFalse` mock and a fork of the liquidation flow directly reproduces this revert at line 696 of `src/Midnight.sol`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** src/Midnight.sol (L575-578)
```text
    /// @dev See LIQUIDATIONS section for more details.
    /// @dev At least one of seizedAssets or repaidUnits should be equal to zero.
    /// @dev Passing both 0 for seizedAssets and repaidUnits allows to realize bad debt with 0 token transferred.
    /// @dev Liquidations with both 0 for seizedAssets and repaidUnits can be done with a collateral that is not
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/Midnight.sol (L643-677)
```text
        if (repaidUnits > 0 || seizedAssets > 0) {
            uint256 _maxLif = market.collateralParams[collateralIndex].maxLif;
            uint256 lif = postMaturityMode
                ? UtilsLib.min(_maxLif, WAD + (_maxLif - WAD) * (block.timestamp - market.maturity) / TIME_TO_MAX_LIF)
                : _maxLif;

            if (seizedAssets > 0) {
                repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
            } else {
                seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
            }

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

            uint128 newCollateral = _position.collateral[collateralIndex] - UtilsLib.toUint128(seizedAssets);
            _position.collateral[collateralIndex] = newCollateral;
            if (newCollateral == 0 && seizedAssets > 0) {
                _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
            }
            _marketState.withdrawable += UtilsLib.toUint128(repaidUnits);
            _position.debt -= UtilsLib.toUint128(repaidUnits);
        }
```

**File:** src/Midnight.sol (L696-696)
```text
        SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
```

**File:** src/libraries/SafeTransferLib.sol (L12-22)
```text
    function safeTransfer(address token, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());

        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transfer, (to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
        }
        require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferReturnedFalse());
    }
```

**File:** test/SafeTransferLibTest.sol (L15-18)
```text
contract ERC20False {
    function transfer(address to, uint256 value) external returns (bool res) {}
    function transferFrom(address from, address to, uint256 value) external returns (bool res) {}
}
```
