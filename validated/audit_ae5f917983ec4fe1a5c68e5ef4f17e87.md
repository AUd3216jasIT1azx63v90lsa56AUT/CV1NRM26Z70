### Title
Paused Collateral Token Blocks Liquidation, Granting Borrowers a Free Look at Prices — (File: src/Midnight.sol)

---

### Summary

When a collateral token (e.g., USDC) is paused, `liquidate` reverts because it unconditionally attempts to transfer seized collateral to the receiver in the same transaction. Unhealthy borrowers cannot be liquidated for the duration of the pause. Because `repay` only pulls the loan token (never the collateral token), borrowers can observe price movements during the pause and selectively repay or default — at the expense of lenders who absorb the resulting bad debt.

---

### Finding Description

In `liquidate`, all state mutations (bad-debt socialization, collateral reduction, debt reduction) are applied first, then the seized collateral is pushed to the receiver unconditionally:

```solidity
// src/Midnight.sol line 696
SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
```

`SafeTransferLib.safeTransfer` propagates any revert from the token call verbatim:

```solidity
// src/libraries/SafeTransferLib.sol lines 15-19
(bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transfer, (to, value)));
if (!success) {
    assembly ("memory-safe") {
        revert(add(returndata, 0x20), mload(returndata))
    }
}
```

If the collateral token is paused (USDC, USDT, and many other regulated ERC-20s implement a `pause()` that causes every `transfer` call to revert), the entire `liquidate` call reverts, rolling back all state changes. The unhealthy position is left intact.

The only escape hatch — calling `liquidate` with `seizedAssets = 0` and `repaidUnits = 0` to realize bad debt — still reaches the same `safeTransfer` call with `value = 0`. Because USDC reverts on **all** transfers (including zero-value ones) when paused, even this path is blocked.

Meanwhile, `repay` is unaffected:

```solidity
// src/Midnight.sol line 520
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
```

`repay` only touches the loan token, never the collateral token. A borrower can therefore repay their debt at any time during the pause, deactivating the unhealthy position before liquidators can act once the token unpauses.

**Exploit flow:**

1. Borrower opens a large position using a pausable collateral token (USDC) as collateral.
2. Collateral price drops; position becomes unhealthy (`debt > maxDebt`).
3. Collateral token is paused (by the token issuer, or front-run by the borrower who monitors governance).
4. Every `liquidate` call reverts at line 696 — no liquidation is possible.
5. Borrower watches prices during the pause:
   - **Prices recover** → position returns to health; borrower suffers no loss.
   - **Prices do not recover** → borrower calls `repay` (loan token only, unaffected by pause) to clear debt, then withdraws collateral after unpause. Alternatively, borrower walks away; bad debt is socialized among lenders via `lossFactor`.
6. When the token unpauses, liquidators can finally act, but the window of maximum loss has already passed.

---

### Impact Explanation

- Unhealthy borrowers are shielded from liquidation for the entire duration of a collateral token pause.
- Borrowers receive an asymmetric option: repay if prices moved against them, do nothing if prices recovered.
- Bad debt that accrues during the pause is socialized among all lenders in the market via `lossFactor`, causing permanent credit dilution.
- The longer the pause, the larger the potential bad debt and the greater the lender loss.

---

### Likelihood Explanation

- USDC (Circle) and USDT (Tether) both implement pausable transfers and are natural collateral choices in any lending protocol.
- USDC has been paused in practice (e.g., during the Tornado Cash sanctions enforcement).
- No privileged access to Midnight is required; the attacker only needs to monitor the collateral token's governance or mempool and front-run the pause with a large borrow.
- Even without intentional exploitation, any borrower whose collateral token happens to be paused while their position is unhealthy passively benefits.

---

### Recommendation

Decouple the collateral transfer from the liquidation state update. Two options:

1. **Two-step liquidation**: Record seized collateral as claimable by the liquidator in a mapping during `liquidate` (state update only, no transfer). Add a separate `claimSeizedCollateral` function that the liquidator calls later to pull the tokens. This mirrors the GMX recommendation exactly.

2. **Try-transfer pattern**: Attempt the collateral transfer; if it fails, record the amount as claimable and continue. This keeps the single-call UX for the common case while remaining robust to paused tokens.

---

### Proof of Concept

**Preconditions:**
- Market with USDC as collateral token, DAI as loan token.
- Borrower has `debt = 1000` units, `maxDebt = 900` (unhealthy).
- USDC is paused.

**Steps:**

```
1. Liquidator calls liquidate(market, collateralIndex=0, seizedAssets=X, repaidUnits=0, borrower, false, receiver, address(0), "")
   → State updates applied (lines 670-676)
   → SafeTransferLib.safeTransfer(USDC, receiver, X) called at line 696
   → USDC.transfer reverts ("Pausable: paused")
   → SafeTransferLib propagates revert (lines 16-19 of SafeTransferLib.sol)
   → Entire liquidate() reverts; state rolled back.

2. Liquidator tries liquidate(..., seizedAssets=0, repaidUnits=0, ...)
   → badDebt block may execute (lines 626-641)
   → SafeTransferLib.safeTransfer(USDC, receiver, 0) called at line 696
   → USDC.transfer(receiver, 0) reverts ("Pausable: paused") — USDC reverts on all transfers when paused
   → Entire call reverts.

3. Borrower calls repay(market, 1000, borrower, address(0), "")
   → Only SafeTransferLib.safeTransferFrom(DAI, borrower, Midnight, 1000) at line 520
   → DAI is not paused; succeeds.
   → Borrower's debt = 0; position is now healthy.

4. USDC unpauses. Liquidators find no unhealthy position to liquidate.
   Borrower withdraws USDC collateral via withdrawCollateral().
```

**Result:** Borrower avoided liquidation entirely by exploiting the window created by the paused collateral token. Lenders bear any bad debt that accumulated during the pause. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/Midnight.sol (L153-158)
```text
/// @dev If a token pulled by Midnight reverts or returns false on transferFrom, take, repay, supplyCollateral,
/// liquidate, and flashLoan repayment revert when they need to pull that token.
/// @dev If a token sent by Midnight reverts or returns false on transfer, withdraw, withdrawCollateral, fee claims,
/// liquidate, and flashLoan revert when they need to send that token.
/// @dev If a callback reverts or returns something other than CALLBACK_SUCCESS, take, repay, liquidate, and flashLoan
/// revert.
```

**File:** src/Midnight.sol (L502-521)
```text
    function repay(Market memory market, uint256 units, address onBehalf, address callback, bytes calldata data)
        external
    {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);

        position[id][onBehalf].debt -= UtilsLib.toUint128(units);
        marketState[id].withdrawable += UtilsLib.toUint128(units);

        address payer = callback != address(0) ? callback : msg.sender;
        emit EventsLib.Repay(msg.sender, id, units, onBehalf, payer);

        if (callback != address(0)) {
            require(
                IRepayCallback(callback).onRepay(id, market, units, onBehalf, data) == CALLBACK_SUCCESS,
                WrongRepayCallbackReturnValue()
            );
        }
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
    }
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
