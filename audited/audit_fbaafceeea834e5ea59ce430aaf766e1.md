Now I have all the information needed for a complete analysis. Let me verify the `tExchange`/`liquidationLocked` scope and the loop ordering.

### Title
Oracle Reentrancy During Liquidation Loop Inflates `maxDebt` via Storage-Read Collateral, Blocking Liquidation — (File: src/Midnight.sol)

### Summary

The `liquidate` function caches `_position.collateralBitmap` in memory but reads `_position.collateral[i]` from storage on every loop iteration. Because `IOracle.price()` is an unchecked external call with no reentrancy guard on `liquidate` or `supplyCollateral`, a malicious oracle deployed by an unprivileged market creator can reenter `supplyCollateral` during the price query for a high-index collateral, inflating the storage value of a lower-index collateral before the loop reads it. This causes `maxDebt` to be computed against post-reentrant collateral amounts, potentially making `originalDebt > maxDebt` false and reverting with `NotLiquidatable`.

### Finding Description

**Exact code path:**

`liquidate` caches the bitmap at line 606 but reads collateral from storage at line 612 on every iteration:

```
uint128 _collateralBitmap = _position.collateralBitmap;   // cached once
while (_collateralBitmap != 0) {
    uint256 i = UtilsLib.msb(_collateralBitmap);           // MSB → highest index first
    ...
    uint256 price = IOracle(_collateralParam.oracle).price(); // external call ← reentry point
    ...
    uint256 _collateral = _position.collateral[i];          // storage read, NOT cached
    maxDebt += _collateral.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(_collateralParam.lltv, WAD);
    ...
    _collateralBitmap = _collateralBitmap.clearBit(i);      // clears local copy only
}
``` [1](#0-0) 

The loop iterates from highest to lowest index (MSB-first via `UtilsLib.msb`). So when processing collateral `i`, all collaterals `j < i` have not yet been read. [2](#0-1) 

`supplyCollateral` has no reentrancy guard and writes directly to `_position.collateral[collateralIndex]` in storage:

```solidity
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets);
``` [3](#0-2) 

The only guard in `supplyCollateral` is the authorization check:

```solidity
require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
``` [4](#0-3) 

This is satisfiable: the borrower calls `setIsAuthorized(maliciousOracle, true, borrower)` before the attack, granting the oracle permission to supply collateral on the borrower's behalf.

The `liquidationLocked` mechanism uses transient storage and is only set during `take` (line 444), never during `liquidate`. It does not block reentrancy from `liquidate` into `supplyCollateral`. [5](#0-4) [6](#0-5) 

The Certora specs explicitly acknowledge this gap: *"Assumes no reentrancy: callbacks and token transfers are not modeled as re-entering Midnight, so re-entrant collateral changes are not covered."* [7](#0-6) 

**Exploit flow:**

1. Attacker deploys a market with two collateral slots: index `1` (malicious oracle) and index `0` (normal oracle).
2. Attacker (as borrower) supplies collateral at both indices and borrows until just below LLTV.
3. Attacker calls `setIsAuthorized(maliciousOracle, true, borrower)`.
4. Attacker pre-funds `maliciousOracle` with collateral-0 tokens and calls `approve(midnight, largeAmount)`.
5. Oracle price drops; borrower becomes unhealthy (`debt > maxDebt`).
6. Liquidator calls `liquidate(market, collateralIndex=1, ...)`.
7. Loop iteration 1 (`i=1`): calls `maliciousOracle.price()`.
   - Oracle reenters: `midnight.supplyCollateral(market, 0, largeAmount, borrower)` — succeeds because oracle is authorized.
   - `_position.collateral[0]` is inflated in storage.
   - Oracle returns a price.
8. Loop iteration 2 (`i=0`): reads `_position.collateral[0]` from storage — **inflated value**.
   - `maxDebt` accumulates the inflated contribution.
9. Post-loop check: `originalDebt > maxDebt` is now `false`.
10. `require(!liquidationLocked(id, borrower) && originalDebt > maxDebt, NotLiquidatable())` — **reverts**. [8](#0-7) 

### Impact Explanation

The scoped impact is concrete: `maxDebt` is computed using the post-reentrant-supply `_position.collateral[j]`, which is larger than the pre-call value. If the reentrant supply is sized so that `collateral[j] * price[j] * lltv[j] / ORACLE_PRICE_SCALE / WAD` pushes `maxDebt >= originalDebt`, the `NotLiquidatable` check fires and the liquidation reverts. An unhealthy borrower can permanently block liquidation by repeating this on every liquidation attempt, violating the core invariant that unhealthy positions remain liquidatable.

### Likelihood Explanation

**Preconditions:**
- Attacker must be both market creator (to set a malicious oracle for collateral `i`) and borrower (or control the borrower account, to call `setIsAuthorized`).
- Attacker must pre-fund the oracle with enough collateral-`j` tokens to push `maxDebt >= debt`.
- The market must have at least two collateral slots with different indices.

All preconditions are achievable by a single unprivileged actor in a permissionless market. The attack is repeatable on every liquidation attempt and costs only the gas for the reentrant `supplyCollateral` call (the attacker gets their collateral-`j` tokens deposited into their own position, so there is no net token loss). The attack is deterministic and not dependent on timing or MEV.

### Recommendation

Cache all `_position.collateral[i]` values before any external call, or add a reentrancy guard (e.g., transient-storage lock) to `liquidate` that prevents `supplyCollateral` from modifying the borrower's position while a liquidation is in progress. The simplest fix consistent with the existing `liquidationLocked` pattern is to set a transient lock at the start of `liquidate` (keyed on `(id, borrower)`) and check it in `supplyCollateral`, reverting if the borrower's position is currently being liquidated. Alternatively, snapshot all collateral amounts into a local memory array before the oracle-calling loop begins.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract MaliciousOracle {
    IMidnight public midnight;
    Market public market;
    address public borrower;
    uint256 public reentrantCollateralIndex; // j < i
    uint256 public reentrantAmount;
    uint256 public returnPrice;

    constructor(IMidnight _midnight, Market memory _market, address _borrower,
                uint256 _j, uint256 _amount, uint256 _price) {
        midnight = _midnight; market = _market; borrower = _borrower;
        reentrantCollateralIndex = _j; reentrantAmount = _amount; returnPrice = _price;
    }

    function price() external returns (uint256) {
        // Reenter supplyCollateral for collateral j (lower index, not yet processed)
        midnight.supplyCollateral(market, reentrantCollateralIndex, reentrantAmount, borrower);
        return returnPrice;
    }
}

// Test plan (Foundry):
// 1. Deploy market with collateral[1] = MaliciousOracle, collateral[0] = NormalOracle.
// 2. borrower.supplyCollateral(market, 1, C1, borrower)
// 3. borrower.supplyCollateral(market, 0, C0, borrower)
// 4. borrower.take(...) to borrow D units (just below LLTV).
// 5. borrower.setIsAuthorized(address(maliciousOracle), true, borrower)
// 6. Fund maliciousOracle with largeAmount of collateral[0] token; approve midnight.
// 7. Drop oracle[0] price so debt > maxDebt (borrower unhealthy).
// 8. vm.expectRevert(IMidnight.NotLiquidatable.selector);
//    liquidator.liquidate(market, 1, 0, repaidUnits, borrower, false, receiver, address(0), "");
// 9. Assert: liquidation reverted with NotLiquidatable despite borrower being unhealthy.
// 10. Assert: midnight.collateral(id, borrower, 0) == C0 + largeAmount (reentrant supply succeeded).
```

**Expected assertions:**
- `liquidate` reverts with `NotLiquidatable` even though `debtOf(id, borrower) > pre-reentrant maxDebt`.
- `collateral(id, borrower, 0)` equals `C0 + largeAmount`, confirming the reentrant write occurred.
- Repeating the liquidation call continues to revert as long as the oracle reenters.

### Citations

**File:** src/Midnight.sol (L444-444)
```text
        bool wasLocked = UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, true);
```

**File:** src/Midnight.sol (L524-545)
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
```

**File:** src/Midnight.sol (L606-618)
```text
        uint128 _collateralBitmap = _position.collateralBitmap;
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

**File:** src/Midnight.sol (L937-939)
```text
    function liquidationLocked(bytes32 id, address user) public view returns (bool) {
        return UtilsLib.tGet(LIQUIDATION_LOCK_SLOT, id, user);
    }
```

**File:** src/libraries/UtilsLib.sol (L53-58)
```text
    /// @dev Assumes bitmap is not zero.
    function msb(uint128 bitmap) internal pure returns (uint256 res) {
        assembly {
            res := sub(255, clz(bitmap))
        }
    }
```

**File:** certora/specs/OnlyAuthorizedCanChange.spec (L73-74)
```text
/// Assumes no reentrancy: callbacks and token transfers are not modeled as re-entering Midnight, so re-entrant collateral changes are not covered.
rule onlyAuthorizedCanChangeCollateralExceptLiquidate(env e, method f, calldataarg args, bytes32 id, address user, uint256 collateralIndex) filtered { f -> f.selector != sig:liquidate(Midnight.Market, uint256, uint256, uint256, address, bool, address, address, bytes).selector } {
```
