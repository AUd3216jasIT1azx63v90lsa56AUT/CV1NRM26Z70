Audit Report

## Title
Returndata Bomb in `SafeTransferLib.safeTransfer` Enables Permanent OOG DoS of `liquidate()` - (File: src/libraries/SafeTransferLib.sol)

## Summary
`SafeTransferLib.safeTransfer` allocates a heap-allocated `bytes memory returndata` buffer that copies the full returndata from a failed `transfer()` call, incurring quadratic EVM memory-expansion gas. Because `touchMarket()` performs no behavioral validation of collateral tokens, an attacker can deploy a malicious token whose `transfer()` reverts with a multi-megabyte payload, making every `liquidate()` call on that market permanently OOG-revert. The unhealthy position and any bad debt become permanently unresolvable, freezing lender funds.

## Finding Description

**Root cause — `SafeTransferLib.safeTransfer` (src/libraries/SafeTransferLib.sol:15-19):**

```solidity
(bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transfer, (to, value)));
if (!success) {
    assembly ("memory-safe") {
        revert(add(returndata, 0x20), mload(returndata))
    }
}
```

The Solidity compiler emits `RETURNDATACOPY` to populate `bytes memory returndata`. The EVM charges quadratic memory-expansion gas: `cost ≈ 3·words + words²/512`. A ~1 MB payload costs ~2.2 M gas; ~4 MB exceeds the 30 M block gas limit entirely, making the call permanently unexecutable regardless of gas supplied. [1](#0-0) 

**Unconditional call in `liquidate()` (src/Midnight.sol:696):**

`safeTransfer` is called unconditionally after all state mutations, including the bad-debt-only path where both `seizedAssets` and `repaidUnits` are zero (the `if (repaidUnits > 0 || seizedAssets > 0)` block at line 643 is skipped, but the transfer at line 696 is not guarded):

```solidity
SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
``` [2](#0-1) 

All preceding state mutations — debt reduction, collateral seizure, `lossFactor` update — at lines 626–677 are rolled back on OOG revert. [3](#0-2) 

**Permissionless market creation with no token behavioral validation (`touchMarket`, src/Midnight.sol:755-791):**

`touchMarket()` only checks address ordering, allowed LLTV tiers, and valid `maxLif` values. There is no check that the collateral token's `transfer()` behaves correctly: [4](#0-3) 

**Existing guard is insufficient (src/libraries/SafeTransferLib.sol:13):**

The `token.code.length > 0` check only verifies bytecode existence, not that `transfer()` behaves correctly: [5](#0-4) 

**Exploit flow:**
1. Attacker deploys `MaliciousToken` — `transferFrom()` succeeds (so collateral can be supplied), `transfer()` always reverts with a ≥ 4 MB payload.
2. Attacker deploys `MaliciousOracle` — initially returns a healthy price.
3. Attacker calls `touchMarket()` with `MaliciousToken` as collateral and `MaliciousOracle` as oracle. All checks pass (address ordering, LLTV tier, maxLif).
4. Attacker supplies `MaliciousToken` as collateral and borrows (attacker can be both maker and taker).
5. Attacker switches `MaliciousOracle` to return a low price → position becomes unhealthy.
6. Every subsequent `liquidate()` call hits `safeTransfer` → `MaliciousToken.transfer()` reverts with the bomb → `bytes memory returndata` expansion OOGs the caller permanently.

## Impact Explanation
Any liquidator calling `liquidate()` on the poisoned market OOGs before the function can complete. The unhealthy position — including any bad debt — can never be resolved. Lenders in the market cannot recover funds through liquidation, and the `lossFactor` update that socializes bad debt is permanently blocked. This constitutes a permanent freeze of lender funds in the affected market. The impact is concrete and irreversible for the affected market.

## Likelihood Explanation
Market creation is fully permissionless and requires no governance approval. The attacker needs only to deploy two cheap contracts (malicious token + oracle), create a market, and open a small borrow position. The attack is repeatable across any number of markets. Any third-party liquidation bot or user attempting to liquidate the position will be griefed. The only cost is initial setup gas.

## Recommendation
Guard the `safeTransfer` call in `liquidate()` with a `seizedAssets > 0` check to avoid calling `transfer(token, receiver, 0)` on the bad-debt-only path. More critically, cap the returndata size before copying it into memory in `SafeTransferLib.safeTransfer` and `safeTransferFrom`, for example by using assembly to limit `RETURNDATACOPY` to a fixed maximum (e.g., 32 bytes), or by using a low-level assembly implementation that avoids allocating a heap buffer for the full returndata. Additionally, consider adding a token behavioral validation step in `touchMarket()` (e.g., a dry-run call or a whitelist) to prevent malicious tokens from being registered as collateral.

## Proof of Concept
1. Deploy `MaliciousToken` with `transferFrom()` returning `true` and `transfer()` reverting with `returndatacopy` of 4 MB of zeros.
2. Deploy `MaliciousOracle` with a configurable price.
3. Call `midnight.touchMarket(market)` with `MaliciousToken` as collateral and `MaliciousOracle` as oracle — succeeds.
4. Call `midnight.supplyCollateral(market, 0, amount, attacker)` — succeeds (uses `transferFrom`).
5. Call `midnight.take(...)` to open a borrow position — succeeds.
6. Call `MaliciousOracle.setPrice(lowPrice)` to make the position unhealthy.
7. Call `midnight.liquidate(market, 0, 0, 0, attacker, false, receiver, address(0), "")` with maximum gas — OOG revert every time, demonstrating permanent DoS.

### Citations

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

**File:** src/Midnight.sol (L626-696)
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

**File:** src/Midnight.sol (L755-791)
```text
    function touchMarket(Market memory market) public returns (bytes32) {
        bytes32 id = toId(market);
        if (marketState[id].tickSpacing == 0) {
            require(market.maturity <= block.timestamp + 100 * 365 days, MaturityTooFar());
            require(market.collateralParams.length > 0, NoCollateralParams());
            require(market.collateralParams.length <= MAX_COLLATERALS, TooManyCollateralParams());
            address previousCollateralToken;
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

            MarketState storage _marketState = marketState[id];
            _marketState.tickSpacing = DEFAULT_TICK_SPACING;
            uint16[7] memory _defaultSettlementFeeCbp = defaultSettlementFeeCbp[market.loanToken];
            _marketState.settlementFeeCbp0 = _defaultSettlementFeeCbp[0];
            _marketState.settlementFeeCbp1 = _defaultSettlementFeeCbp[1];
            _marketState.settlementFeeCbp2 = _defaultSettlementFeeCbp[2];
            _marketState.settlementFeeCbp3 = _defaultSettlementFeeCbp[3];
            _marketState.settlementFeeCbp4 = _defaultSettlementFeeCbp[4];
            _marketState.settlementFeeCbp5 = _defaultSettlementFeeCbp[5];
            _marketState.settlementFeeCbp6 = _defaultSettlementFeeCbp[6];
            _marketState.continuousFee = defaultContinuousFee[market.loanToken];
            IdLib.storeInCode(market, INITIAL_CHAIN_ID);

            emit EventsLib.MarketCreated(market, id);
        }
        return id;
    }
```
