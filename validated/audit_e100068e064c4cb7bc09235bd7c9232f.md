### Title
Borrower Can Permanently Block Liquidation by Activating a Collateral with a Rogue Reverting Oracle — (`src/Midnight.sol`)

### Summary

Market creation in Midnight is permissionless, and each collateral slot carries its own oracle address fixed at creation time. A borrower who controls one of those oracle contracts can activate that collateral slot (even with 1 wei) just before a liquidation attempt, then make the oracle revert. Because `liquidate()` calls `IOracle(...).price()` for every activated collateral without any try-catch, the entire liquidation transaction reverts, permanently shielding the borrower from liquidation and forcing bad debt onto lenders.

### Finding Description

**Root cause — unguarded oracle call inside `liquidate()`**

In `liquidate()`, the protocol iterates over every bit in `_position.collateralBitmap` and calls `price()` on each collateral's oracle with no error handling: [1](#0-0) 

```solidity
while (_collateralBitmap != 0) {
    uint256 i = UtilsLib.msb(_collateralBitmap);
    CollateralParams memory _collateralParam = market.collateralParams[i];
    uint256 price = IOracle(_collateralParam.oracle).price();   // ← no try-catch
    ...
    _collateralBitmap = _collateralBitmap.clearBit(i);
}
```

If any single oracle reverts, the whole `liquidate()` call reverts.

**Activation vector — `supplyCollateral()` never calls the oracle**

The contract itself documents this gap:

> "Note that a borrower can activate a collateral once its oracle is reverting because the oracle is not called in `supplyCollateral`." [2](#0-1) 

`supplyCollateral()` only transfers the token and sets the bitmap bit; it never queries the oracle: [3](#0-2) 

This means a borrower can activate a collateral whose oracle is already (or will be made) reverting, and the protocol has no way to prevent it.

**End-to-end exploit flow**

1. Attacker deploys `MaliciousOracle` — a contract with a `setRevert(bool)` toggle that initially returns a valid price.
2. Attacker calls `touchMarket()` to create a permissionless market with:
   - Collateral A: legitimate token + legitimate oracle (used for actual borrowing)
   - Collateral B: any ERC-20 token + `MaliciousOracle`
3. Lenders sign offers for the market (the oracle looks legitimate at this point).
4. Attacker takes sell offers, borrowing against collateral A.
5. Collateral A price drops; position becomes unhealthy.
6. **Before the liquidation tx lands**, attacker front-runs with:
   - `supplyCollateral(market, collateralIndexB, 1, attacker)` — activates collateral B with 1 wei
   - `MaliciousOracle.setRevert(true)` — makes the oracle revert
7. Liquidator's `liquidate()` call hits the `while` loop, calls `MaliciousOracle.price()`, reverts.
8. Attacker can later toggle the oracle back to normal, withdraw collateral B (deactivating it), and repeat the cycle as needed.

**Why `withdrawCollateral` cannot rescue the situation**

`withdrawCollateral()` calls `isHealthy()`, which also iterates over all activated collaterals and calls `price()` on each oracle: [4](#0-3) 

While the malicious oracle is reverting, `isHealthy()` reverts too, so `withdrawCollateral()` reverts — the attacker cannot accidentally deactivate the shield collateral while the oracle is in revert mode. The attacker controls the toggle and can flip it back at will.

### Impact Explanation

- The borrower's unhealthy position cannot be liquidated for as long as the malicious oracle is in revert mode.
- Bad debt accumulates in the market and is socialized among lenders via the `lossFactor` mechanism.
- Lenders suffer direct, unrecoverable capital loss proportional to the borrower's outstanding debt.
- The attack is repeatable: the attacker can toggle the oracle, withdraw collateral B, replenish their position, and repeat.

### Likelihood Explanation

- **No privileged access required.** Market creation is fully permissionless; anyone can deploy an oracle and create a market.
- **Deception is realistic.** The malicious oracle behaves correctly until the attacker needs to block liquidation, making it indistinguishable from a legitimate oracle during normal operation.
- **Front-running is straightforward.** The attacker monitors the mempool for liquidation transactions and front-runs with two cheap transactions (supply 1 wei + toggle oracle).
- **Economic incentive is strong.** A large borrower can save their entire collateral-backed position at the cost of deploying a few contracts.

### Recommendation

Wrap the `IOracle(...).price()` call inside `liquidate()` in a try-catch, and treat a reverting oracle as a price of zero (or skip that collateral from the health/bad-debt computation). This mirrors the recommendation in the referenced Telcoin report for `requiresNotification()`:

```diff
- uint256 price = IOracle(_collateralParam.oracle).price();
+ uint256 price;
+ try IOracle(_collateralParam.oracle).price() returns (uint256 p) {
+     price = p;
+ } catch {
+     revert OracleReverted(_collateralParam.oracle);
+ }
```

Alternatively, enforce that a collateral can only be activated if its oracle is currently returning a valid (non-reverting) price at the time of `supplyCollateral()`. This closes the activation vector entirely.

### Proof of Concept

```solidity
// MaliciousOracle.sol
contract MaliciousOracle {
    bool public shouldRevert;
    address public owner;
    constructor() { owner = msg.sender; }
    function setRevert(bool v) external { require(msg.sender == owner); shouldRevert = v; }
    function price() external view returns (uint256) {
        require(!shouldRevert, "oracle disabled");
        return 1e18; // valid price initially
    }
}

// Attack sequence (pseudo-code):
// 1. Deploy MaliciousOracle mo
// 2. Create market: collaterals = [CollateralParams(tokenA, lltv, maxLif, legitimateOracle),
//                                   CollateralParams(tokenB, lltv, maxLif, address(mo))]
// 3. Borrow against tokenA (position becomes unhealthy when tokenA price drops)
// 4. Front-run liquidation:
//    midnight.supplyCollateral(market, 1 /*collateralIndexB*/, 1, attacker);
//    mo.setRevert(true);
// 5. Liquidator's liquidate() reverts — attacker is safe
// 6. mo.setRevert(false) to restore normal state when convenient
```

### Citations

**File:** src/Midnight.sol (L35-36)
```text
/// @dev Note that a borrower can activate a collateral once its oracle is reverting because the oracle is not called in
/// supplyCollateral.
```

**File:** src/Midnight.sol (L524-546)
```text
    function supplyCollateral(Market memory market, uint256 collateralIndex, uint256 assets, address onBehalf)
        external
    {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        address collateralToken = market.collateralParams[collateralIndex].token;

        Position storage _position = position[id][onBehalf];
        uint256 oldCollateral = _position.collateral[collateralIndex];
        _position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets);

        if (oldCollateral == 0 && assets > 0) {
            uint128 newCollateralBitmap = _position.collateralBitmap.setBit(collateralIndex);
            _position.collateralBitmap = newCollateralBitmap;
            require(
                UtilsLib.countBits(newCollateralBitmap) <= MAX_COLLATERALS_PER_BORROWER, TooManyActivatedCollaterals()
            );
        }

        emit EventsLib.SupplyCollateral(msg.sender, id, collateralToken, assets, onBehalf);

        SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
    }
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
