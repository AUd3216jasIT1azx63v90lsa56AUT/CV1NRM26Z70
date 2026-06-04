### Title
Oracle Price Front-Running in `withdrawCollateral` Enables Bad Debt Creation — (File: src/Midnight.sol)

### Summary
The `withdrawCollateral` function in `src/Midnight.sol` performs a health check using a live spot oracle price. Because oracle price updates (e.g., Chainlink heartbeat transactions) are visible in the public mempool before inclusion, a borrower positioned near the health boundary can front-run a pending price drop, withdraw collateral while still healthy at the current price, and leave an undercollateralised position that creates bad debt for lenders.

### Finding Description

**Root cause**

`withdrawCollateral` reduces the borrower's on-chain collateral balance and then calls `isHealthy`, which reads the oracle price at that instant:

```solidity
// src/Midnight.sol:561-568
uint256 newCollateral = _position.collateral[collateralIndex] - assets;
_position.collateral[collateralIndex] = UtilsLib.toUint128(newCollateral);
...
require(isHealthy(market, id, onBehalf), UnhealthyBorrower());
```

`isHealthy` fetches the spot price from the oracle:

```solidity
// src/Midnight.sol:953
uint256 price = IOracle(collateralParam.oracle).price();
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
    .mulDivDown(collateralParam.lltv, WAD);
```

There is no TWAP, no price-change guard, and no withdrawal delay. The health check is therefore only as fresh as the oracle's last on-chain update.

**Exploit flow**

1. Attacker holds a borrow position where `collateral × P_current × LLTV / ORACLE_PRICE_SCALE ≈ debt` (near the health boundary).
2. Attacker monitors the public mempool and observes a pending oracle update that will lower the collateral price from `P_current` to `P_new < P_current`.
3. Attacker submits `withdrawCollateral(assets = X)` with a higher gas price, front-running the oracle update.
4. At `P_current` the health check passes: `(C − X) × P_current × LLTV ≥ debt`.
5. The oracle update lands; price becomes `P_new`.
6. Now `(C − X) × P_new × LLTV < debt` — the position is unhealthy.
7. When a liquidator eventually liquidates, the `badDebt` path in `liquidate` is triggered, socialising the loss across all lenders via `lossFactor`.

The same window exists in `take` when the attacker is the seller (increasing debt), but `withdrawCollateral` is the cleaner extraction path because the attacker directly receives collateral tokens.

### Impact Explanation

Bad debt is created and socialised among all lenders in the market through the `lossFactor` mechanism:

```solidity
// src/Midnight.sol:631-634
_marketState.lossFactor = UtilsLib.toUint128(
    type(uint128).max - (type(uint128).max - _lossFactor)
        .mulDivDown(_totalUnits - badDebt, _totalUnits)
);
_marketState.totalUnits -= UtilsLib.toUint128(badDebt);
```

Every lender's credit is silently reduced at their next interaction. The attacker retains the withdrawn collateral with no obligation to cover the shortfall. Severity: **Medium** — direct, quantifiable loss of lender funds; magnitude scales with position size and price-drop delta.

### Likelihood Explanation

- Chainlink and similar oracle networks broadcast price-update transactions publicly before inclusion; the new price is readable from the pending transaction's calldata.
- Any borrower with a position near the LLTV boundary (a common state for capital-efficient borrowers) can execute this with standard MEV tooling — no privileged keys, no flash loan, no oracle compromise required.
- The attack is profitable whenever the gas cost of the front-run is less than `X × (P_current − P_new)` in loan-token terms.
- Likelihood: **Medium-High** on chains with a public mempool (Ethereum mainnet, most L2s with a sequencer mempool).

### Recommendation

1. **TWAP oracle**: Require oracles to expose a time-weighted price so a single pending update cannot be exploited atomically.
2. **Withdrawal buffer**: Apply a small health-factor buffer (e.g., require `maxDebt ≥ debt × (1 + buffer)`) on `withdrawCollateral` to absorb small price moves.
3. **Slippage guard in periphery**: `MidnightBundles` already accepts a `minSellerAssets` guard; a similar `minHealthFactor` parameter on `repayAndWithdrawCollateral` would let integrators protect users.

### Proof of Concept

```
Setup:
  LLTV = 0.86e18, ORACLE_PRICE_SCALE = 1e36
  Collateral C = 1000 tokens, oracle price P_current = 1e36 (1:1)
  maxDebt = 1000 × 1e36 × 0.86 / 1e36 = 860
  debt = 859  →  position is healthy by 1 unit

Pending oracle update: P_new = 0.90e36 (−10%)
  maxDebt_new = 1000 × 0.90e36 × 0.86 / 1e36 = 774  →  unhealthy

Attack:
  1. Attacker sees pending oracle tx in mempool.
  2. Calls withdrawCollateral(assets = 86):
       newCollateral = 914
       maxDebt_check = 914 × 1e36 × 0.86 / 1e36 = 786 ≥ 859? NO
     → Adjust: withdraw assets = 1 (minimal extraction to stay healthy):
       newCollateral = 999
       maxDebt_check = 999 × 1e36 × 0.86 / 1e36 = 859.14 ≥ 859 ✓  (passes)
  3. Oracle lands: P_new = 0.90e36
       maxDebt_new = 999 × 0.90e36 × 0.86 / 1e36 = 773.2 < 859  →  unhealthy
  4. Liquidator calls liquidate; badDebt = 859 − 773 = 86 units
     socialised across all lenders via lossFactor.
  5. Attacker keeps 1 collateral token extracted for free.

Scale: with C = 1,000,000 tokens and a 10% price drop, bad debt ≈ 86,000 units.
``` [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** src/Midnight.sol (L549-573)
```text
    function withdrawCollateral(
        Market memory market,
        uint256 collateralIndex,
        uint256 assets,
        address onBehalf,
        address receiver
    ) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        address collateralToken = market.collateralParams[collateralIndex].token;

        Position storage _position = position[id][onBehalf];
        uint256 newCollateral = _position.collateral[collateralIndex] - assets;
        _position.collateral[collateralIndex] = UtilsLib.toUint128(newCollateral);

        if (newCollateral == 0 && assets > 0) {
            _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
        }

        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());

        emit EventsLib.WithdrawCollateral(msg.sender, id, collateralToken, assets, onBehalf, receiver);

        SafeTransferLib.safeTransfer(collateralToken, receiver, assets);
    }
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

**File:** src/Midnight.sol (L944-960)
```text
    function isHealthy(Market memory market, bytes32 id, address borrower) public view returns (bool) {
        Position storage _position = position[id][borrower];
        uint256 debt = _position.debt;
        uint256 maxDebt;
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
        }
        return maxDebt >= debt;
    }
```
