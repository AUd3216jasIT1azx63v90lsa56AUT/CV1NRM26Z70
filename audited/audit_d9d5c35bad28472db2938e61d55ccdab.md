### Title
Gas Griefing via Malicious Oracle Returndata Blocks Liquidation — (File: src/Midnight.sol)

---

### Summary

`Midnight.sol` calls `IOracle(collateralParam.oracle).price()` inside `liquidate()` and `isHealthy()` using a plain high-level Solidity call with no gas cap and no inline-assembly guard. Because markets are created permissionlessly and the oracle address is never validated, an attacker can supply a malicious oracle that returns an arbitrarily large returndata payload. Solidity's ABI decoder unconditionally executes `returndatacopy` over the entire payload, consuming gas proportional (and quadratically for memory expansion) to its size, causing the calling transaction to run out of gas and revert. This permanently blocks liquidation of the attacker's position, forcing bad debt onto lenders.

---

### Finding Description

**Root cause — no returndata guard on oracle calls**

`touchMarket()` creates markets permissionlessly and performs zero validation on the oracle address supplied in each `CollateralParams` entry: [1](#0-0) 

Any address, including a contract the attacker fully controls, is accepted as an oracle.

Inside `liquidate()`, for every activated collateral the protocol calls: [2](#0-1) 

Inside `isHealthy()` (which is also called at the tail of `take()` to verify the seller's health): [3](#0-2) 

Both calls are plain high-level Solidity calls. The Solidity compiler emits a `returndatacopy` that copies **all** bytes returned by the callee into memory before the ABI decoder reads the first 32 bytes. There is no `{gas: limit}` cap and no inline-assembly wrapper to skip the copy.

**Exploit mechanics**

A malicious oracle implements `price()` to return, e.g., 300 kB of padding after the legitimate `uint256`. The cost of `returndatacopy` is ~3 gas per 32-byte word, but memory expansion is quadratic: expanding memory to ~300 kB costs on the order of several million gas. With enough padding the call site exhausts the transaction's remaining gas and reverts.

**Attack path**

1. Attacker deploys `MaliciousOracle` — initially returns a correct `uint256` price so the market looks legitimate; a flag controlled by the attacker switches it to return 300 kB+ of data.
2. Attacker calls `touchMarket()` with `MaliciousOracle` as the oracle for one collateral. No check prevents this.
3. Lenders observe a functioning market and provide liquidity via `take()`.
4. Attacker borrows (increases debt) against the collateral.
5. Attacker's position becomes unhealthy (price drops, or attacker deliberately under-collateralises).
6. Attacker flips the oracle flag so `price()` now returns a huge payload.
7. Any call to `liquidate()` hits line 610, the oracle returns 300 kB, `returndatacopy` exhausts gas, the transaction reverts.
8. `isHealthy()` (line 953) also reverts, so the health check at the end of `take()` (line 476) reverts too, freezing the entire market for sellers with debt. [4](#0-3) 

---

### Impact Explanation

- **Liquidation permanently blocked**: no liquidator can repay the attacker's debt; the position accumulates bad debt.
- **Bad debt socialised to lenders**: `lossFactor` is updated on bad-debt realisation, slashing every lender's credit proportionally.
- **Market frozen for sellers**: `take()` calls `isHealthy()` for any seller with debt; if that seller's oracle is malicious, all `take()` calls for that seller revert, freezing their side of the market.
- **Direct financial loss** to lenders who provided liquidity in good faith.

---

### Likelihood Explanation

- Market creation is **fully permissionless** — no governance approval, no oracle whitelist.
- The oracle can behave correctly during the "attract liquidity" phase and switch behaviour only when the attacker needs to block liquidation; lenders have no on-chain protection against this.
- The attacker needs only a standard EOA and the gas to deploy two contracts (oracle + market creation). No privileged role is required.
- The same oracle can be reused across multiple markets, amplifying impact.

---

### Recommendation

Wrap every oracle call in an inline-assembly block that reads only the first 32 bytes of returndata without triggering a full `returndatacopy`, mirroring the fix applied in PancakeSwap V4 (`d507e09`):

```solidity
function _safeOraclePrice(address oracle) internal view returns (uint256 price) {
    bool success;
    assembly ("memory-safe") {
        mstore(0x00, 0x13966db5) // price() selector
        success := staticcall(gas(), oracle, 0x1c, 0x04, 0x00, 0x20)
        price := mload(0x00)
    }
    require(success, OracleCallFailed());
}
```

This prevents the ABI decoder from copying an arbitrarily large returndata payload into memory. Apply the same pattern to `IEnterGate` and `ILiquidatorGate` calls at lines 399, 404, and 598, which are subject to the identical class of attack. [5](#0-4) [6](#0-5) 

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

contract MaliciousOracle {
    bool public griefMode;
    uint256 public storedPrice = 1e18;

    function enableGrief() external { griefMode = true; }

    function price() external view returns (uint256) {
        if (griefMode) {
            // Return legitimate uint256 followed by ~300 kB of zeros.
            // returndatacopy in the caller will copy all of it, exhausting gas.
            assembly {
                let ptr := mload(0x40)
                mstore(ptr, 1000000000000000000) // valid price word
                return(ptr, 307200)              // 300 kB total
            }
        }
        return storedPrice;
    }
}
```

**Steps**:
1. Deploy `MaliciousOracle`.
2. Call `Midnight.touchMarket()` with `MaliciousOracle` as `collateralParams[0].oracle`.
3. Have a second account supply liquidity and a third account borrow.
4. Call `MaliciousOracle.enableGrief()`.
5. Attempt `Midnight.liquidate(...)` with sufficient gas (e.g. 3 000 000).
6. Observe the transaction reverts with out-of-gas at the `IOracle(...).price()` call on line 610.
7. The borrower's unhealthy position remains open; bad debt is eventually socialised to lenders. [7](#0-6) [8](#0-7)

### Citations

**File:** src/Midnight.sol (L397-406)
```text
        require(
            offer.market.enterGate == address(0) || buyerCreditIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseCredit(buyer),
            BuyerGatedFromIncreasingCredit()
        );
        require(
            offer.market.enterGate == address(0) || sellerDebtIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseDebt(seller),
            SellerGatedFromIncreasingDebt()
        );
```

**File:** src/Midnight.sol (L474-477)
```text
        }
        if (!wasLocked) UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, false);
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());

```

**File:** src/Midnight.sol (L598-600)
```text
            market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
            LiquidatorGatedFromLiquidating()
        );
```

**File:** src/Midnight.sol (L607-618)
```text
        while (_collateralBitmap != 0) {
            uint256 i = UtilsLib.msb(_collateralBitmap);
            CollateralParams memory _collateralParam = market.collateralParams[i];
            uint256 price = IOracle(_collateralParam.oracle).price();
            if (i == collateralIndex) liquidatedCollatPrice = price;
            uint256 _collateral = _position.collateral[i];
            maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(_collateralParam.lltv, WAD);
            badDebt = badDebt.zeroFloorSub(
                _collateral.mulDivUp(price, ORACLE_PRICE_SCALE).mulDivUp(WAD, _collateralParam.maxLif)
            );
            _collateralBitmap = _collateralBitmap.clearBit(i);
        }
```

**File:** src/Midnight.sol (L762-772)
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
```

**File:** src/Midnight.sol (L948-957)
```text
        if (debt > 0) {
            uint128 _collateralBitmap = _position.collateralBitmap;
            while (_collateralBitmap != 0) {
                uint256 i = UtilsLib.msb(_collateralBitmap);
                CollateralParams memory collateralParam = market.collateralParams[i];
                uint256 price = IOracle(collateralParam.oracle).price();
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
                _collateralBitmap = _collateralBitmap.clearBit(i);
            }
```
