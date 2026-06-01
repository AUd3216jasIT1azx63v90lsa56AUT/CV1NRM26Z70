### Title
Division-by-zero revert in `liquidate` when `collateralIndex` is absent from borrower's `collateralBitmap` and `repaidUnits > 0` - ([File: src/Midnight.sol])

### Summary
`liquidatedCollatPrice` is initialized to `0` and is only assigned inside the bitmap loop when `i == collateralIndex`. If the caller supplies a `collateralIndex` that is not set in the borrower's `collateralBitmap`, the assignment at line 611 is never reached, leaving `liquidatedCollatPrice = 0`. When `repaidUnits > 0`, the subsequent computation at line 652 divides by this zero value, causing an unconditional arithmetic revert. No existing guard checks that `collateralIndex` is present in the borrower's bitmap before entering the seize/repay branch.

### Finding Description
**Code path:** [1](#0-0) 

`liquidatedCollatPrice` starts at `0`. The loop iterates only the bits set in `_position.collateralBitmap`. The assignment `liquidatedCollatPrice = price` at line 611 fires only when `i == collateralIndex`. [2](#0-1) 

The three entry-point guards check: (1) at most one of `repaidUnits`/`seizedAssets` is non-zero, (2) borrower has debt, (3) liquidator gate. None of them verify that `collateralIndex` is set in `_position.collateralBitmap`. [3](#0-2) 

The `NotLiquidatable` guard checks health/maturity but still does not validate `collateralIndex` membership. [4](#0-3) 

When `repaidUnits > 0` the `else` branch at line 652 executes:
```
seizedAssets = repaidUnits.mulDivDown(lif, WAD).mulDivDown(ORACLE_PRICE_SCALE, liquidatedCollatPrice);
//                                                                                ^^^^^^^^^^^^^^^^^^^^ == 0 → revert
```
`ORACLE_PRICE_SCALE / 0` is a division-by-zero, reverting the entire transaction.

**Attacker-controlled inputs:** `collateralIndex` (any index absent from the borrower's bitmap), `repaidUnits > 0`, `seizedAssets = 0`.

**Exploit flow:**
1. Borrower has debt > 0 and collateral only at index 0 (bitmap = `0x1`).
2. Position becomes unhealthy (`originalDebt > maxDebt`) or post-maturity.
3. Liquidator calls `liquidate(market, 1, 0, repaidUnits=1, borrower, false, receiver, address(0), "")`.
4. Bitmap loop iterates only index 0; `liquidatedCollatPrice` is never set → remains `0`.
5. `NotLiquidatable` check passes (position is genuinely unhealthy).
6. `badDebt` block may execute (state changes, but all rolled back on revert).
7. Line 652: `mulDivDown(ORACLE_PRICE_SCALE, 0)` → arithmetic revert.

### Impact Explanation
Any caller can make their own `liquidate` transaction revert with a raw arithmetic panic instead of a protocol-defined error. While a correctly-written liquidator passes a valid `collateralIndex`, the protocol emits no guard that enforces this, so any liquidator bot or contract that does not independently pre-validate bitmap membership will receive an opaque revert. The unhealthy position remains unliquidated for that call. Because the revert rolls back all state (including any partial `badDebt` accounting), there is no persistent state corruption, but the liquidation opportunity is lost for that transaction.

### Likelihood Explanation
Preconditions are easily met: any unhealthy or post-maturity borrower with at least one collateral index suffices. The liquidator only needs to supply a `collateralIndex` value not present in the borrower's bitmap alongside `repaidUnits > 0`. This is reachable by any unprivileged address with no special setup, and is repeatable indefinitely.

### Recommendation
Add an explicit check immediately after the bitmap loop (or before the seize/repay branch) that `liquidatedCollatPrice != 0` when `repaidUnits > 0 || seizedAssets > 0`, or equivalently require that `collateralIndex` is set in `_position.collateralBitmap`:

```solidity
if (repaidUnits > 0 || seizedAssets > 0) {
    require(
        _position.collateralBitmap.hasBit(collateralIndex),
        InvalidCollateralIndex()
    );
    // ... existing lif / seize / repay logic
}
```

This produces a meaningful revert instead of a division-by-zero and closes the missing-validation gap.

### Proof of Concept
```solidity
// Foundry unit test
function test_liquidate_divisionByZero_collateralIndexNotInBitmap() public {
    // Setup: create market with collateral at index 0 and index 1
    // Borrower supplies collateral only at index 0, borrows to unhealthy LTV
    // Confirm borrower.collateralBitmap has only bit 0 set
    // Confirm position is unhealthy (debt > maxDebt)

    vm.prank(liquidator);
    // collateralIndex=1 (not in bitmap), repaidUnits=1, seizedAssets=0
    vm.expectRevert(); // arithmetic panic (division by zero)
    midnight.liquidate(
        market,
        1,       // collateralIndex NOT in borrower bitmap
        0,       // seizedAssets
        1,       // repaidUnits > 0
        borrower,
        false,
        receiver,
        address(0),
        ""
    );

    // Assert: position is still unhealthy and unliquidated
    // Assert: borrower.debt unchanged (full rollback)
    // Assert: marketState.totalUnits unchanged
}
```
Expected assertion: transaction reverts with arithmetic panic at `Midnight.sol:652`. A follow-up call with `collateralIndex=0` (correct index) must succeed, confirming the position was liquidatable and only the invalid-index path is broken.

### Citations

**File:** src/Midnight.sol (L595-600)
```text
        require(UtilsLib.atMostOneNonZero(repaidUnits, seizedAssets), InconsistentInput());
        require(_position.debt > 0, NotBorrower()); // to avoid no-op liquidations of non borrower positions.
        require(
            market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
            LiquidatorGatedFromLiquidating()
        );
```

**File:** src/Midnight.sol (L603-611)
```text
        uint256 liquidatedCollatPrice;
        uint256 originalDebt = _position.debt;
        uint256 badDebt = originalDebt;
        uint128 _collateralBitmap = _position.collateralBitmap;
        while (_collateralBitmap != 0) {
            uint256 i = UtilsLib.msb(_collateralBitmap);
            CollateralParams memory _collateralParam = market.collateralParams[i];
            uint256 price = IOracle(_collateralParam.oracle).price();
            if (i == collateralIndex) liquidatedCollatPrice = price;
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/Midnight.sol (L643-653)
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
```
