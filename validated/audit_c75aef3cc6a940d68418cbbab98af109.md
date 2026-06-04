### Title
Pull-Model Oracle Price Can Be Manipulated Intra-Transaction to Drain Collateral or Profitably Liquidate Healthy Positions — (`src/Midnight.sol`)

---

### Summary

Morpho Midnight's `IOracle` interface imposes no staleness constraint, no per-block update limit, and no timestamp validation on the price it consumes. Because the protocol is permissionless and any address can be set as a collateral oracle, markets that use pull-model oracles (e.g., Pyth, Stork) are vulnerable to the same sandwich attack described in the external report: an attacker atomically updates the oracle price, interacts with the protocol at the manipulated price, and profits — all within a single transaction.

---

### Finding Description

**Root cause**

`IOracle` exposes only a single `price()` view: [1](#0-0) 

There is no timestamp, no heartbeat, no per-block update guard. The oracle address is stored in `CollateralParams.oracle`: [2](#0-1) 

It is fixed at market creation and immutable thereafter. The protocol is fully permissionless — any address, including a live Pyth or Stork feed, can be supplied as the oracle.

**Where the price is consumed**

`isHealthy()` calls the oracle directly: [3](#0-2) 

`withdrawCollateral()` enforces health only via `isHealthy()`: [4](#0-3) 

`take()` enforces health at the end of execution: [5](#0-4) 

`liquidate()` uses the oracle price to determine liquidatability and to compute seized assets: [6](#0-5) [7](#0-6) 

**`multicall()` enables atomic composition** [8](#0-7) 

An attacker can batch an oracle update call (via the oracle's own permissionless update function, e.g., Pyth's `updatePriceFeeds`) with any Midnight entry-point in a single transaction.

---

### Impact Explanation

**Attack A — Excess collateral withdrawal (theft from lenders)**

1. Attacker holds a position with debt `D` and collateral `C`. At real price `P`, `maxDebt = C·P·LLTV`.
2. In one transaction: update oracle to `P' = P·(1+δ)` → call `withdrawCollateral()` to remove `ΔC` such that the position is still healthy at `P'` but undercollateralized at `P`.
3. Health check at line 568 passes at `P'`; oracle reverts to `P` after the tx.
4. Position is now undercollateralized. Lenders absorb the bad debt via `lossFactor` socialization.

**Attack B — Profitable liquidation of a healthy position (theft from borrowers)**

1. Attacker identifies a position healthy at real price `P`.
2. In one transaction: update oracle to `P' = P·(1-δ)` → call `liquidate()`.
3. At `P'`, `maxDebt` is lower, so the position appears unhealthy (line 622).
4. `seizedAssets = repaidUnits · LIF / P'` — because `P'` is artificially low, the attacker seizes more collateral per loan token repaid (line 652).
5. Attacker sells seized collateral at real price `P`, pocketing `repaidUnits · LIF · δ / (1-δ)` in profit.

Both attacks require no privileged role. The only precondition is a market whose oracle is a pull-model feed.

---

### Likelihood Explanation

- Pyth Network is the dominant pull-model oracle on EVM chains; its `updatePriceFeeds` is permissionless and callable by anyone.
- Morpho Midnight is fully permissionless — any market creator can deploy a market with a Pyth feed as the collateral oracle.
- The attack is atomic (single transaction), MEV-bot friendly, and requires no upfront capital beyond the trade itself (flash loans can fund the repaid units in Attack B via the `liquidate` callback).
- The attack is most profitable during high volatility, exactly when pull-model oracle prices diverge most from the last on-chain update.

---

### Recommendation

1. **Require a timestamp/staleness bound in the oracle interface.** Extend `IOracle` to return `(uint256 price, uint256 updatedAt)` and revert if `block.timestamp - updatedAt > MAX_STALENESS`.
2. **Limit oracle price updates to once per block.** If the protocol integrates pull-model oracles natively, record `lastUpdateBlock` and reject a second update in the same block.
3. **Use TWAP or push-model oracles for collateral pricing.** Chainlink price feeds update asynchronously and cannot be manipulated intra-transaction.
4. **Document the oracle trust assumption explicitly.** If pull-model oracles are intentionally supported, warn market creators that using them without an intra-block update guard exposes the market to this attack.

---

### Proof of Concept

```
// Attack B: profitable liquidation of a healthy position
// Precondition: market uses a Pyth oracle; victim has debt D, collateral C, healthy at real price P.

contract Exploit is ILiquidateCallback {
    IPyth pyth;
    IMidnight midnight;
    Market market;
    address victim;

    function attack(bytes[] calldata pythUpdateData) external payable {
        // Step 1: push a stale/lower price to the Pyth feed (permissionless)
        pyth.updatePriceFeeds{value: msg.value}(pythUpdateData);
        // Oracle now returns P' < P

        // Step 2: liquidate the victim — position is now "unhealthy" at P'
        // seizedAssets = repaidUnits * LIF / P'  (inflated because P' is low)
        midnight.liquidate(
            market,
            0,          // collateralIndex
            0,          // seizedAssets (use repaidUnits input)
            repaidUnits,
            victim,
            false,
            address(this),
            address(this), // callback to flash-fund repaidUnits
            ""
        );
        // Step 3: sell seized collateral at real price P → profit
    }

    function onLiquidate(...) external returns (bytes4) {
        // flash-fund repaidUnits from a flash loan or own balance
        IERC20(loanToken).transfer(address(midnight), repaidUnits);
        return CALLBACK_SUCCESS;
    }
}
```

The key lines in `Midnight.sol` that execute without any intra-block oracle freshness guard: [9](#0-8) [10](#0-9) [7](#0-6)

### Citations

**File:** src/interfaces/IOracle.sol (L5-7)
```text
interface IOracle {
    function price() external view returns (uint256);
}
```

**File:** src/interfaces/IMidnight.sol (L14-19)
```text
struct CollateralParams {
    address token;
    uint256 lltv;
    uint256 maxLif;
    address oracle;
}
```

**File:** src/Midnight.sol (L211-220)
```text
    function multicall(bytes[] calldata calls) external {
        for (uint256 i = 0; i < calls.length; i++) {
            (bool success, bytes memory returnData) = address(this).delegatecall(calls[i]);
            if (!success) {
                assembly ("memory-safe") {
                    revert(add(returnData, 0x20), mload(returnData))
                }
            }
        }
    }
```

**File:** src/Midnight.sol (L475-477)
```text
        if (!wasLocked) UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, false);
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());

```

**File:** src/Midnight.sol (L548-568)
```text
    /// @dev This function does not call any oracle if the borrower has no debt.
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

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/Midnight.sol (L649-653)
```text
            if (seizedAssets > 0) {
                repaidUnits = seizedAssets.mulDivUp(liquidatedCollatPrice, ORACLE_PRICE_SCALE).mulDivUp(WAD, lif);
            } else {
                seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
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
