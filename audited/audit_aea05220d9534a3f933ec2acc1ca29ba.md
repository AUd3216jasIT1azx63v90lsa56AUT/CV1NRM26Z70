### Title
Liquidation of unhealthy positions permanently blocked when collateral token returns false on transfer - (File: src/libraries/SafeTransferLib.sol)

### Summary
`SafeTransferLib.safeTransfer` reverts with `TransferReturnedFalse()` when the collateral token's `transfer()` call succeeds at the EVM level but returns `false`. Because `liquidate()` performs all state mutations (collateral reduction, debt reduction, withdrawable increase) before calling `safeTransfer`, a false-return ERC20 collateral causes every liquidation attempt to revert and roll back, permanently trapping an unhealthy position in an unliquidatable state. Market creation is permissionless and performs no validation on collateral token transfer behavior.

### Finding Description
**Exact code path:**

`liquidate()` in `src/Midnight.sol` lines 670–676 mutates state first: [1](#0-0) 

Then at line 696 it calls: [2](#0-1) 

Inside `SafeTransferLib.safeTransfer`, line 21 enforces: [3](#0-2) 

If the collateral token's `transfer()` returns `false` (non-reverting), `success` is `true` but `abi.decode(returndata, (bool))` is `false`, so the `require` fires `TransferReturnedFalse()`. The entire transaction reverts, rolling back all state changes from lines 670–676.

**Market creation has no token validation.** `touchMarket()` only checks collateral token address ordering, allowed LLTV, and valid `maxLif`: [4](#0-3) 

There is no check that the collateral token's `transfer()` returns `true` or behaves as a standard ERC20.

**Exploit flow:**
1. Attacker (market creator) deploys a false-return ERC20 — `transfer()` always returns `false` without reverting.
2. Attacker calls `touchMarket` (via any entry point) with this token as `collateralParams[i].token`. Market is created successfully.
3. A borrower supplies the false-return token as collateral and takes on debt.
4. Oracle price drops; position becomes unhealthy (`debt > maxDebt`).
5. Any liquidator calls `liquidate()`. The `NotLiquidatable()` check passes (line 620–624). State is mutated (lines 670–676). `safeTransfer` is called at line 696.
6. Token returns `false` → `TransferReturnedFalse()` revert → entire transaction rolls back.
7. Position remains unhealthy and permanently unliquidatable. No liquidator can ever succeed.

**Why existing checks fail:** The `NotLiquidatable()` guard correctly identifies the position as liquidatable. The `LiquidatorGatedFromLiquidating()` check is irrelevant (gate is optional). There is no guard anywhere in the call path that validates the collateral token's transfer return value before state mutation, nor any allowlist preventing false-return tokens from being registered as collateral.

### Impact Explanation
A genuinely unhealthy borrower's position can never be liquidated. The protocol's core invariant — "unhealthy positions remain liquidatable" — is permanently violated for any market whose collateral token returns `false` on `transfer()`. Lenders in such a market cannot recover their funds through liquidation, and bad debt cannot be socialized. The position is frozen in an unhealthy state indefinitely.

### Likelihood Explanation
Market creation is fully permissionless; any address can call `touchMarket` with an arbitrary collateral token. Deploying a false-return ERC20 requires no special privilege. The condition is deterministic and repeatable: every liquidation attempt on such a market will revert. The attacker does not need to take any action after market creation — the token's static behavior is sufficient. The precondition (unhealthy position) is reachable via normal oracle price movement or maturity passage.

### Recommendation
Add a transfer-return validation at market creation time in `touchMarket`. Before accepting a collateral token, perform a zero-value probe transfer (or a `staticcall` to `transfer`) and verify it returns `true` or returns no data. Alternatively, maintain a protocol-level allowlist of validated collateral tokens, rejecting any token not on the list. A simpler mitigation is to add a `try/catch` or a pre-flight check in `liquidate()` that skips the `TransferReturnedFalse` revert path and instead records the seized collateral as claimable by the liquidator in a separate mapping, preserving liveness.

### Proof of Concept
```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {IERC20} from "src/interfaces/IERC20.sol";

/// @dev ERC20 that always returns false on transfer (non-reverting)
contract FalseReturnToken {
    mapping(address => uint256) public balanceOf;
    function mint(address to, uint256 amt) external { balanceOf[to] += amt; }
    function transfer(address, uint256) external pure returns (bool) { return false; }
    function transferFrom(address from, address to, uint256 amt) external returns (bool) {
        balanceOf[from] -= amt; balanceOf[to] += amt; return true;
    }
    function approve(address, uint256) external pure returns (bool) { return true; }
}

contract FalseReturnLiquidationTest is Test {
    Midnight midnight;
    FalseReturnToken collateral;
    // ... setup market, oracle, loan token as in existing test harness

    function testLiquidateRevertsOnFalseReturnCollateral() public {
        // 1. Deploy false-return ERC20 as collateral
        collateral = new FalseReturnToken();

        // 2. Create market with false-return token as collateral (touchMarket succeeds)
        // 3. Borrower supplies collateral (transferFrom returns true — succeeds)
        // 4. Borrower takes debt
        // 5. Drop oracle price to make position unhealthy
        // 6. Assert liquidate reverts with TransferReturnedFalse
        vm.expectRevert(SafeTransferLib.TransferReturnedFalse.selector);
        midnight.liquidate(market, 0, 0, repaidUnits, borrower, false, address(this), address(0), "");

        // 7. Assert position is still unhealthy and unchanged
        assertGt(midnight.debtOf(id, borrower), 0);
        // invariant: unhealthy position remains unliquidatable — VIOLATED
    }
}
```

Expected assertion: `vm.expectRevert(SafeTransferLib.TransferReturnedFalse.selector)` passes, confirming every liquidation attempt reverts. A follow-up invariant test asserting `!isHealthy → liquidate succeeds` would fail for this market. [5](#0-4) [6](#0-5)

### Citations

**File:** src/Midnight.sol (L581-600)
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
        bytes32 id = touchMarket(market);
        MarketState storage _marketState = marketState[id];
        Position storage _position = position[id][borrower];
        require(UtilsLib.atMostOneNonZero(repaidUnits, seizedAssets), InconsistentInput());
        require(_position.debt > 0, NotBorrower()); // to avoid no-op liquidations of non borrower positions.
        require(
            market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
            LiquidatorGatedFromLiquidating()
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

**File:** src/Midnight.sol (L696-696)
```text
        SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
```

**File:** src/Midnight.sol (L762-773)
```text
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
