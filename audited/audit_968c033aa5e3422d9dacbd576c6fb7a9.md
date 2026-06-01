### Title
Unconditional zero-amount `safeTransfer` in `liquidate()` blocks bad-debt-only liquidations for zero-revert collateral tokens - (`File: src/Midnight.sol`)

### Summary
`liquidate()` unconditionally calls `SafeTransferLib.safeTransfer(collateralToken, receiver, seizedAssets)` at line 696 even when `seizedAssets = 0`, directly contradicting the NatSpec at line 577 which states "Passing both 0 for seizedAssets and repaidUnits allows to realize bad debt with 0 token transferred." For any market whose collateral token reverts on zero-amount transfers, the bad-debt-only liquidation path (`seizedAssets = 0, repaidUnits = 0`) is permanently DoS'd.

### Finding Description

**Exact code path:**

`liquidate()` accepts `seizedAssets = 0, repaidUnits = 0` through the guard at line 595:

```solidity
require(UtilsLib.atMostOneNonZero(repaidUnits, seizedAssets), InconsistentInput());
``` [1](#0-0) 

The block at line 643 is skipped entirely when both inputs are zero:

```solidity
if (repaidUnits > 0 || seizedAssets > 0) { ... }
``` [2](#0-1) 

So `seizedAssets` remains `0`. Execution then falls through unconditionally to:

```solidity
SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
``` [3](#0-2) 

`SafeTransferLib.safeTransfer` has no zero-amount guard — it always issues the low-level `token.call`:

```solidity
(bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transfer, (to, value)));
if (!success) {
    assembly ("memory-safe") { revert(add(returndata, 0x20), mload(returndata)) }
}
``` [4](#0-3) 

**Root cause:** Missing `if (seizedAssets > 0)` guard before the `safeTransfer` call at line 696.

**Attacker inputs / exploit flow:**
1. Market creator (listed as unprivileged attacker in scope) deploys a market whose `collateralParams[i].token` is a token that reverts on zero-amount `transfer()` calls (a known real-world pattern).
2. A borrower takes a position; the collateral price drops such that `badDebt > 0`.
3. Any liquidator calls `liquidate(market, collateralIndex, 0, 0, borrower, false, receiver, address(0), "")`.
4. Bad-debt accounting at lines 626–641 executes correctly and modifies `_position.debt` and `_marketState`.
5. Execution reaches line 696: `safeTransfer(collateralToken, receiver, 0)` → collateral token reverts → entire transaction reverts.
6. All state changes are rolled back. Bad debt is never realized.

**Why existing checks fail:** The `atMostOneNonZero` check at line 595 explicitly permits both-zero inputs. No downstream guard prevents the zero-amount transfer. The Certora rule `liquidateLossFactorDoesNotRevert` (line 104 of `LossFactor.spec`) proves non-reversion under this path but abstracts away ERC20 external calls via summaries, so it does not catch this. [5](#0-4) 

### Impact Explanation

Bad-debt-only liquidations (`seizedAssets = 0, repaidUnits = 0`) are permanently blocked for any market whose collateral token reverts on zero-amount transfers. The bad debt is never socialized: `_marketState.lossFactor` is never updated, lenders cannot recover proportional losses, and the borrower's debt is never reduced. This violates the core invariant that "every credit has matching debt or valid settled/loss state" and the protocol's own documented guarantee at line 577. [6](#0-5) 

### Likelihood Explanation

**Preconditions:**
- A market exists with a collateral token that reverts on zero-amount `transfer()`. Several deployed ERC20 tokens exhibit this behavior (e.g., LEND, some rebasing or fee-on-transfer tokens with zero-amount guards).
- The borrower's position has `badDebt > 0` (collateral value insufficient to cover debt at `maxLif`).

**Feasibility:** The market creator role is unprivileged per the audit scope. Once such a market exists and a position reaches bad-debt state, the DoS is triggered by any liquidator on every attempt. It is repeatable and permanent until the collateral token is replaced (impossible post-deployment). No special timing or oracle manipulation is required.

### Recommendation

Add a zero-amount guard before the collateral `safeTransfer` call, and symmetrically before the loan token `safeTransferFrom`:

```solidity
// Line 696 — guard the collateral transfer
if (seizedAssets > 0) {
    SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
}

// Line 717 — guard the loan token transfer
if (repaidUnits > 0) {
    SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
}
``` [7](#0-6) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
// ... standard test imports

/// @dev ERC20 that reverts on zero-amount transfer
contract ZeroRevertCollateral {
    mapping(address => uint256) public balanceOf;
    function transfer(address to, uint256 amount) external returns (bool) {
        require(amount > 0, "zero transfer");   // <-- the trigger
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }
    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        require(amount > 0, "zero transfer");
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
    function mint(address to, uint256 amount) external { balanceOf[to] += amount; }
}

contract BadDebtDoSTest is Test {
    function testBadDebtLiquidationDoS() public {
        // 1. Deploy Midnight and create market with ZeroRevertCollateral
        ZeroRevertCollateral col = new ZeroRevertCollateral();
        // ... set up market, oracle, loanToken as in existing tests

        // 2. Borrower supplies collateral and takes debt
        // ... collateralize(market, borrower, units)
        // ... setupMarket(market, units)

        // 3. Drop oracle price to create bad debt
        // oracle.setPrice(badDebtPrice);
        // assert(_badDebt() > 0);

        // 4. Liquidator calls bad-debt-only liquidation
        vm.expectRevert("zero transfer");
        midnight.liquidate(
            market,
            0,       // collateralIndex
            0,       // seizedAssets = 0
            0,       // repaidUnits = 0
            borrower,
            false,
            address(this),
            address(0),
            ""
        );

        // 5. Assert bad debt was NOT realized (state unchanged)
        // assertEq(midnight.debtOf(id, borrower), units);
        // assertEq(midnight.lossFactor(id), 0);
    }
}
```

**Expected assertion:** The call reverts with `"zero transfer"` from the collateral token, confirming that `safeTransfer(collateralToken, receiver, 0)` is reached and reverts before any state is committed. The borrower's debt and the market's `lossFactor` remain unchanged, proving the DoS.

### Citations

**File:** src/Midnight.sol (L577-577)
```text
    /// @dev Passing both 0 for seizedAssets and repaidUnits allows to realize bad debt with 0 token transferred.
```

**File:** src/Midnight.sol (L595-595)
```text
        require(UtilsLib.atMostOneNonZero(repaidUnits, seizedAssets), InconsistentInput());
```

**File:** src/Midnight.sol (L643-643)
```text
        if (repaidUnits > 0 || seizedAssets > 0) {
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

**File:** src/libraries/SafeTransferLib.sol (L15-19)
```text
        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transfer, (to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
```

**File:** certora/specs/LossFactor.spec (L89-107)
```text
/// The loss factor arithmetic in liquidate does not revert under valid state. Uses seizedAssets=0, repaidUnits=0 to isolate the bad debt realization path. Uses collateralBitmap=0 to skip the collateral loop, ensuring badDebt == position.debt.
rule liquidateLossFactorDoesNotRevert(env e, Midnight.Market market, address borrower, bytes data) {
    bytes32 id = summaryToId(market);

    require data.length == 0, "no callback to avoid unrelated external call reverts";
    require marketIsCreated(market), "market must be created";
    require market.liquidatorGate == 0, "Assumption:no liquidator gate";
    require market.collateralParams.length > 0, "market has at least one collateral (enforced by touchMarket)";
    require !liquidationLocked(id, borrower), "liquidation not locked (transient storage is zero at transaction start)";
    require currentContract.position[id][borrower].collateralBitmap == 0, "Assumption: no active collaterals: skip loop and maximize badDebt";
    require currentContract.position[id][borrower].debt > 0, "borrower must have debt to enter badDebt > 0 block";
    require currentContract.position[id][borrower].debt <= currentContract.marketState[id].totalUnits, "position debt bounded by totalUnits (see totalUnitsEqualsSumNegativeDebtPlusWithdrawable)";
    require e.msg.value == 0, "Midnight is not payable";

    address zero = 0;
    liquidate@withrevert(e, market, 0, 0, 0, borrower, false, borrower, zero, data);

    assert !lastReverted, "liquidate should not revert under valid state (bad debt realization path)";
}
```
