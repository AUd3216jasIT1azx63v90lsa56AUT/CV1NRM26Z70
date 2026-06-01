### Title
Unconditional `safeTransfer` of collateral after state updates permanently freezes liquidations when `collateralToken.transfer` returns false - (File: src/Midnight.sol)

### Summary
`liquidate()` applies all state mutations (bad debt, collateral, withdrawable, debt) at lines 626–676, then unconditionally calls `SafeTransferLib.safeTransfer(collateralToken, receiver, seizedAssets)` at line 696 — even when `seizedAssets == 0`. `SafeTransferLib.safeTransfer` reverts with `TransferReturnedFalse` whenever the token's `transfer` call returns `false`, causing the entire transaction to revert and roll back every state change. Because the call is unconditional, no liquidation path — including pure bad-debt realization with both inputs zero — can succeed against such a market, permanently freezing the position.

### Finding Description
**Code path:**

`liquidate()` (`src/Midnight.sol:581–720`) executes in this order:

1. **Lines 626–676** — storage writes: `_position.debt`, `_marketState.lossFactor`, `_marketState.totalUnits`, `_marketState.continuousFeeCredit`, `_position.collateral[collateralIndex]`, `_position.collateralBitmap`, `_marketState.withdrawable`.
2. **Line 696** — `SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets)` — called with no guard on `seizedAssets`.
3. Lines 698–717 — callback and loan-token pull.

`SafeTransferLib.safeTransfer` (`src/libraries/SafeTransferLib.sol:12–22`) always issues a low-level `call` to the token regardless of `value`, then executes:

```solidity
require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferReturnedFalse());
```

If the token returns `false`, this `require` reverts, unwinding all storage writes from step 1.

**Root cause:** The `safeTransfer` at line 696 is not guarded by `if (seizedAssets > 0)`. The protocol's own comment at line 577 states that passing both inputs as zero "allows to realize bad debt with 0 token transferred," but the unconditional transfer call contradicts this: even a zero-seized-assets call invokes `transfer(receiver, 0)` on the collateral token, which a false-returning token will answer with `false`, triggering `TransferReturnedFalse`.

**Attacker-controlled inputs / preconditions:**
- A market creator (unprivileged) deploys a collateral token whose `transfer` always returns `false` but whose `transferFrom` returns `true` (so `supplyCollateral` succeeds). This is a well-known non-standard ERC-20 pattern.
- A borrower supplies that collateral and takes debt, creating an unhealthy position.
- Any liquidator calls `liquidate()` with any combination of `seizedAssets`/`repaidUnits` (including both zero).

**Why existing checks do not stop it:**
- `require(UtilsLib.atMostOneNonZero(repaidUnits, seizedAssets))` at line 595 permits both-zero inputs.
- `require(!liquidationLocked(...) && ...)` at line 620 passes for an unhealthy position.
- There is no validation that the collateral token is a well-behaved ERC-20.
- `SafeTransferLib` intentionally reverts on false return (line 21) — this is by design for normal tokens but becomes a denial-of-service vector here. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
Every call to `liquidate()` against a market whose `collateralToken.transfer` returns `false` reverts unconditionally, regardless of the `seizedAssets`/`repaidUnits` values chosen. Bad debt cannot be realized, collateral cannot be seized, and the unhealthy position cannot be closed. This violates the core invariant "unhealthy positions remain liquidatable" and leaves bad debt permanently unrealizable, causing lender losses with no recovery path.

### Likelihood Explanation
The precondition — a collateral token that returns `false` on `transfer` but `true` on `transferFrom` — is a known non-standard pattern (e.g., some older or custom ERC-20s, tokens with transfer-pause logic that returns `false` instead of reverting). The market creator role is unprivileged and permissionless. Once such a market exists with active borrowers, the freeze is permanent and repeatable: every liquidation attempt by any liquidator will revert. The condition is not self-correcting.

### Recommendation
Guard the collateral `safeTransfer` with a zero-value check, consistent with the stated intent that both-zero inputs realize bad debt with no token movement:

```solidity
// src/Midnight.sol, replace line 696
if (seizedAssets > 0) {
    SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
}
```

This mirrors the pattern already used elsewhere in the codebase where transfers are conditional on non-zero amounts, and matches the documented behavior at line 577. [4](#0-3) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import "../src/Midnight.sol";

/// Collateral token: transferFrom returns true (deposit works),
/// transfer always returns false (seizure/bad-debt realization fails).
contract FalseTransferCollateral {
    mapping(address => mapping(address => uint256)) public allowance;
    mapping(address => uint256) public balanceOf;

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }
    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true; // deposit succeeds
    }
    function transfer(address, uint256) external pure returns (bool) {
        return false; // always fails
    }
    function mint(address to, uint256 amount) external { balanceOf[to] += amount; }
}

contract LiquidationFreezeTest is Test {
    // Setup: deploy Midnight, create market with FalseTransferCollateral,
    // supply collateral, borrow, drop oracle price to make position unhealthy.

    function testLiquidationFrozen() public {
        // 1. Deploy FalseTransferCollateral as collateralToken.
        // 2. Create market with this token.
        // 3. Borrower supplies collateral (transferFrom=true, succeeds).
        // 4. Borrower takes debt.
        // 5. Drop oracle price so position is unhealthy.
        // 6. Liquidator calls liquidate() with seizedAssets > 0.
        //    ASSERT: reverts with TransferReturnedFalse.
        // 7. Liquidator calls liquidate() with seizedAssets=0, repaidUnits=0 (bad-debt only).
        //    ASSERT: also reverts with TransferReturnedFalse.
        // 8. Assert position.debt > 0 and position is still unhealthy after both attempts.
        //    => liveness invariant violated: unhealthy position is permanently unliquidatable.
    }
}
```

**Expected assertions:**
- Both `liquidate()` calls revert with `SafeTransferLib.TransferReturnedFalse`.
- `midnight.debtOf(id, borrower) > 0` after all attempts.
- `midnight.collateral(id, borrower, 0)` unchanged.
- `midnight.lossFactor(id)` unchanged (bad debt not realized). [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** src/Midnight.sol (L577-580)
```text
    /// @dev Passing both 0 for seizedAssets and repaidUnits allows to realize bad debt with 0 token transferred.
    /// @dev Liquidations with both 0 for seizedAssets and repaidUnits can be done with a collateral that is not
    /// activated.
    /// @dev Returns the seized assets and the repaid units.
```

**File:** src/Midnight.sol (L626-676)
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
```

**File:** src/Midnight.sol (L696-696)
```text
        SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
```

**File:** src/libraries/SafeTransferLib.sol (L9-10)
```text
    error TransferFromReturnedFalse();
    error TransferReturnedFalse();
```

**File:** src/libraries/SafeTransferLib.sol (L15-21)
```text
        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transfer, (to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
        }
        require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferReturnedFalse());
```

**File:** test/SafeTransferLibTest.sol (L15-18)
```text
contract ERC20False {
    function transfer(address to, uint256 value) external returns (bool res) {}
    function transferFrom(address from, address to, uint256 value) external returns (bool res) {}
}
```

**File:** test/SafeTransferLibTest.sol (L83-86)
```text
    function testSafeTransferReturnedFalse() public {
        vm.expectRevert(SafeTransferLib.TransferReturnedFalse.selector);
        this.safeTransfer(address(tokenFalse), address(1), 1);
    }
```
