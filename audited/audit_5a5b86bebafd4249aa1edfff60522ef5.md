### Title
Unconditional `safeTransfer(collateral, receiver, 0)` in `liquidate` blocks bad debt realization for tokens that revert on zero-value transfers - (`File: src/Midnight.sol`)

### Summary
When `liquidate` is called with `seizedAssets=0` and `repaidUnits=0` to realize bad debt, the call at line 696 — `SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets)` — executes unconditionally with `seizedAssets=0`. If the collateral token reverts on zero-value transfers, the entire transaction reverts, undoing the bad debt state changes already written to storage. This directly contradicts the protocol's own NatSpec at line 577: *"Passing both 0 for seizedAssets and repaidUnits allows to realize bad debt with 0 token transferred."*

### Finding Description

**Exact code path:**

In `src/Midnight.sol`, the `liquidate` function:

1. Lines 626–641: Bad debt is realized — `_position.debt`, `_marketState.lossFactor`, `_marketState.totalUnits`, and `_marketState.continuousFeeCredit` are all mutated in storage. [1](#0-0) 

2. Lines 643–677: The collateral/repayment block is **guarded** by `if (repaidUnits > 0 || seizedAssets > 0)` and is correctly skipped when both are zero. [2](#0-1) 

3. **Line 696: `SafeTransferLib.safeTransfer` is called unconditionally, outside any guard, with `seizedAssets` still equal to `0`.** [3](#0-2) 

4. Line 717: `SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits)` is also called unconditionally with `repaidUnits=0`, creating a symmetric issue on the loan token side. [4](#0-3) 

**Root cause in `SafeTransferLib.safeTransfer`:**

The library makes an unconditional low-level `call` to `token.transfer(to, 0)`. If the token's `transfer` returns `success=false`, the assembly block re-reverts with the token's error data, propagating the revert up through `liquidate`. [5](#0-4) 

**Why existing checks do not stop it:**

The `atMostOneNonZero` check at line 595 only validates that at most one of `repaidUnits`/`seizedAssets` is nonzero — it explicitly permits both being zero (the bad debt path). The `_position.debt > 0` check at line 596 is satisfied by the bad debt precondition. No guard exists on the `safeTransfer` call itself. [6](#0-5) 

**Formal verification gap:**

The Certora spec `certora/specs/LossFactor.spec` rule `liquidateLossFactorDoesNotRevert` (lines 89–106) explicitly tests `liquidate(0, 0, 0, borrower, ...)` and asserts it must not revert — but it stubs out `SafeTransferLib.safeTransfer` as `NONDET`, meaning the proof holds only under the assumption that transfers never revert. The real-world token behavior is not covered. [7](#0-6) 

**Attacker-controlled inputs:**

- `seizedAssets = 0`, `repaidUnits = 0` (both caller-controlled)
- `collateralIndex` pointing to the reverting token (caller-controlled)
- `borrower` with `debt > 0` and `collateralValue < debt` (precondition, not attacker-created)

The attacker is any unprivileged liquidator. No special role is required.

### Impact Explanation

When the collateral token reverts on zero-value transfers, every call to `liquidate(0, 0)` reverts. Because the bad debt state writes (lines 628–640) occur before the `safeTransfer` at line 696, they are all rolled back on revert. Concretely:

- `_marketState.lossFactor` is never updated → lenders' credit is never slashed proportionally
- `_marketState.totalUnits` is never reduced → the accounting invariant `totalUnits == sum(debts)` is broken
- The borrower's `_position.debt` is never reduced → the position remains in a permanently bad-debt state
- The market becomes permanently insolvent: no liquidation path can succeed for this borrower

### Likelihood Explanation

**Preconditions:**
1. A market must exist whose collateral token reverts on zero-value transfers. This is a known behavior of some deployed ERC20 tokens (e.g., tokens that enforce `amount > 0`). In a permissionless protocol, any market creator can deploy such a market, intentionally or not.
2. A borrower in that market must have `collateralValue < debt` (bad debt state), which is a normal market condition reachable through price movement.

**Feasibility:** Both preconditions are independently reachable without any privileged action. The attack is repeatable: every subsequent `liquidate(0, 0)` call will also revert, making the block permanent.

### Recommendation

Guard both unconditional transfer calls with zero-amount checks:

```solidity
// Line 696 — guard the collateral transfer
if (seizedAssets > 0) {
    SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
}

// Line 717 — guard the loan token transfer
if (repaidUnits > 0) {
    SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
}
```

This matches the existing guard at line 643 (`if (repaidUnits > 0 || seizedAssets > 0)`) and aligns with the documented intent at line 577. [8](#0-7) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";

/// @dev ERC20 that reverts on zero-value transfers (known real-world pattern)
contract RevertOnZeroToken {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address to, uint256 amount) external { balanceOf[to] += amount; }

    function transfer(address to, uint256 amount) external returns (bool) {
        require(amount > 0, "zero transfer");  // <-- the trigger
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        require(amount > 0, "zero transfer");
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }
}

contract BadDebtBlockTest is Test {
    // Setup: deploy Midnight with a market using RevertOnZeroToken as collateral.
    // Create a borrower position, drop oracle price to create bad debt.

    function testBadDebtRealizationBlockedByZeroTransferRevert() public {
        // 1. Deploy RevertOnZeroToken as collateral token
        // 2. Create market with this collateral token
        // 3. Borrower supplies collateral, borrows units
        // 4. Oracle price drops so collateralValue < debt (bad debt state)
        // 5. Assert _badDebt() > 0

        // 6. Call liquidate(market, 0, 0, 0, borrower, false, receiver, address(0), "")
        vm.expectRevert("zero transfer");  // token's revert propagates
        midnight.liquidate(market, 0, 0, 0, borrower, false, address(this), address(0), "");

        // 7. Assert lossFactor was NOT updated (state rolled back)
        assertEq(midnight.lossFactor(id), initialLossFactor, "lossFactor must not change");

        // 8. Assert totalUnits was NOT reduced
        assertEq(midnight.totalUnits(id), initialTotalUnits, "totalUnits must not change");

        // 9. Assert borrower debt was NOT reduced
        assertEq(midnight.debtOf(id, borrower), initialDebt, "debt must not change");

        // 10. Confirm no alternative liquidation path exists for this borrower
        //     (any call with seizedAssets=0 hits the same revert)
    }
}
```

**Expected assertions:**
- `vm.expectRevert("zero transfer")` passes — the call reverts with the token's error
- `lossFactor == initialLossFactor` — bad debt was not socialized
- `totalUnits == initialTotalUnits` — accounting invariant broken
- `debtOf(borrower) == initialDebt` — position permanently stuck

### Citations

**File:** src/Midnight.sol (L575-578)
```text
    /// @dev See LIQUIDATIONS section for more details.
    /// @dev At least one of seizedAssets or repaidUnits should be equal to zero.
    /// @dev Passing both 0 for seizedAssets and repaidUnits allows to realize bad debt with 0 token transferred.
    /// @dev Liquidations with both 0 for seizedAssets and repaidUnits can be done with a collateral that is not
```

**File:** src/Midnight.sol (L595-596)
```text
        require(UtilsLib.atMostOneNonZero(repaidUnits, seizedAssets), InconsistentInput());
        require(_position.debt > 0, NotBorrower()); // to avoid no-op liquidations of non borrower positions.
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

**File:** src/Midnight.sol (L643-643)
```text
        if (repaidUnits > 0 || seizedAssets > 0) {
```

**File:** src/Midnight.sol (L696-696)
```text
        SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
```

**File:** src/Midnight.sol (L717-717)
```text
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
```

**File:** src/libraries/SafeTransferLib.sol (L15-20)
```text
        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transfer, (to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
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
