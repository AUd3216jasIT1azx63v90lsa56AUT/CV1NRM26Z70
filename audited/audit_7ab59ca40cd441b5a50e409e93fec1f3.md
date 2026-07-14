### Title
Permissionless `liquidate()` Enables Frontrun-DOS on Borrower's `repay()` — (File: src/Midnight.sol)

---

### Summary

The `repay()` function in `Midnight.sol` performs an unchecked subtraction `position[id][onBehalf].debt -= UtilsLib.toUint128(units)`. Because `liquidate()` is permissionless and reduces a borrower's debt, an attacker can frontrun any `repay(units = fullDebt)` call with a dust liquidation (`repaidUnits = 1`), causing the borrower's subtraction to underflow and revert. The attacker can repeat this indefinitely at near-zero cost, permanently preventing a liquidatable borrower from repaying.

---

### Finding Description

**Root cause — `repay()` line 508:**

```solidity
position[id][onBehalf].debt -= UtilsLib.toUint128(units);
```

The function accepts a caller-supplied `units` value and subtracts it directly from the stored debt with no guard against the debt having changed since the caller read it. [1](#0-0) 

**Attack vector — `liquidate()` is permissionless:**

`liquidate()` requires no authorization to call against any liquidatable borrower (unhealthy or post-maturity). It reduces the borrower's debt at line 676:

```solidity
_position.debt -= UtilsLib.toUint128(repaidUnits);
``` [2](#0-1) 

The only gate is an optional `liquidatorGate`; for markets with `liquidatorGate == address(0)` (the default, unrestricted case), any address can liquidate. [3](#0-2) 

**Exploit flow:**

1. Borrower is unhealthy (`debt > maxDebt`) or post-maturity. They read `debt = D` and submit `repay(units = D)`.
2. Attacker sees the pending transaction and frontruns with `liquidate(borrower, repaidUnits = 1, seizedAssets = 0)`.
3. `liquidate()` reduces `position.debt` from `D` to `D - 1` and transfers 1 wei of loan token from the attacker.
4. The borrower's `repay(units = D)` executes: `(D-1) -= D` underflows → Solidity 0.8 reverts.
5. Attacker repeats on every retry.

**Cost to attacker per frontrun:**

With `repaidUnits = 1`, `seizedAssets` is computed as:

```
seizedAssets = 1 * lif / WAD * ORACLE_PRICE_SCALE / liquidatedCollatPrice
```

`ORACLE_PRICE_SCALE = 1e36`. For a typical collateral price (e.g., ETH at ~$3000 → `liquidatedCollatPrice ≈ 3e21`), `seizedAssets ≈ 3.67e14` wei of collateral (worth ~$0.000001). The attacker pays 1 wei of loan token and receives a dust amount of collateral — effectively free. [4](#0-3) 

---

### Impact Explanation

A liquidatable borrower is permanently unable to repay their full debt. Every attempt to call `repay(units = currentDebt)` is griefed at negligible cost. The borrower cannot exit their position cleanly, leading to:

- Continued exposure to liquidation, with the attacker (or others) seizing collateral at the LIF premium.
- Potential total loss of collateral if the position deteriorates further while the borrower is locked out of repaying.
- Permanent freeze of the borrower's ability to self-rescue.

---

### Likelihood Explanation

- **Precondition:** Borrower must be liquidatable (unhealthy or post-maturity). This is a realistic, common state in any lending protocol.
- **Attacker capability:** No privileged access required. Any EOA or contract can call `liquidate()` on an ungated market.
- **Cost:** ~1 wei of loan token + gas per frontrun. On L2s (Midnight targets EVM), gas is cheap enough to sustain indefinite griefing.
- **Motivation:** A competitor, a liquidator wanting to seize collateral at a higher LIF later, or a pure griefer.

---

### Recommendation

Add a `min` cap in `repay()` so that `units` is silently capped to the current debt, or allow `type(uint256).max` as a sentinel for "repay all":

```solidity
uint128 currentDebt = position[id][onBehalf].debt;
uint128 repayUnits = units == type(uint256).max
    ? currentDebt
    : UtilsLib.toUint128(units);
position[id][onBehalf].debt = currentDebt - repayUnits; // safe: repayUnits <= currentDebt
marketState[id].withdrawable += repayUnits;
```

Alternatively, use `UtilsLib.min(units, position[id][onBehalf].debt)` — the same pattern already used elsewhere in the contract (e.g., `sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit)` in `take()`). [5](#0-4) 

---

### Proof of Concept

```
Setup:
  - Market with loanToken=USDC, collateral=WETH, lltv=0.77e18, no liquidatorGate
  - borrower has debt = 1000e18 units, position is unhealthy (oracle price dropped)

Step 1: borrower calls repay(market, 1000e18, borrower, address(0), "")
  → pending in mempool

Step 2: attacker frontruns with:
  liquidate(market, 0, 0, 1, borrower, false, attacker, address(0), "")
  → position[id][borrower].debt becomes 999999999999999999999 (1000e18 - 1)
  → attacker pays 1 wei USDC, receives ~3.67e14 wei WETH

Step 3: borrower's repay executes:
  position[id][borrower].debt -= 1000e18
  → 999999999999999999999 - 1000000000000000000000 → underflow → REVERT

Step 4: attacker repeats on every retry.
  Borrower is permanently unable to repay their full debt.
``` [6](#0-5) [7](#0-6)

### Citations

**File:** src/Midnight.sol (L383-383)
```text
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
```

**File:** src/Midnight.sol (L502-509)
```text
    function repay(Market memory market, uint256 units, address onBehalf, address callback, bytes calldata data)
        external
    {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);

        position[id][onBehalf].debt -= UtilsLib.toUint128(units);
        marketState[id].withdrawable += UtilsLib.toUint128(units);
```

**File:** src/Midnight.sol (L597-600)
```text
        require(
            market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
            LiquidatorGatedFromLiquidating()
        );
```

**File:** src/Midnight.sol (L620-677)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );

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
```

**File:** src/libraries/ConstantsLib.sol (L8-9)
```text
uint256 constant WAD = 1e18;
uint256 constant ORACLE_PRICE_SCALE = 1e36;
```
