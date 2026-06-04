### Title
Reverting Collateral Oracle Permanently Locks Borrower Collateral and Blocks Liquidation/Bad-Debt Realization — (`src/Midnight.sol`)

### Summary

When any activated collateral's oracle begins reverting, both `withdrawCollateral` and `liquidate` become permanently blocked for the affected borrower. Because there is no mechanism to deactivate a collateral slot without withdrawing it, an underwater borrower whose only escape is liquidation is permanently locked, and bad debt can never be socialized to lenders. This is the direct structural analog to the Aave LTV=0 issue: an external state change (oracle going dark) removes the protocol's ability to perform core operations, with no recovery path.

### Finding Description

**Root cause — `isHealthy` iterates every activated collateral unconditionally**

`isHealthy` (called by both `withdrawCollateral` and `take`) loops over the full `collateralBitmap` and calls `IOracle(collateralParam.oracle).price()` for every activated slot whenever `debt > 0`:

```
// src/Midnight.sol:948-957
if (debt > 0) {
    uint128 _collateralBitmap = _position.collateralBitmap;
    while (_collateralBitmap != 0) {
        uint256 i = UtilsLib.msb(_collateralBitmap);
        CollateralParams memory collateralParam = market.collateralParams[i];
        uint256 price = IOracle(collateralParam.oracle).price();   // ← reverts here
        ...
    }
}
``` [1](#0-0) 

`withdrawCollateral` calls `isHealthy` after updating state:

```
// src/Midnight.sol:568
require(isHealthy(market, id, onBehalf), UnhealthyBorrower());
``` [2](#0-1) 

**Root cause — `liquidate` also iterates every activated collateral unconditionally**

The oracle loop in `liquidate` runs for all activated collaterals regardless of whether `seizedAssets` and `repaidUnits` are both zero (the "realize bad debt only" path):

```
// src/Midnight.sol:607-618
uint128 _collateralBitmap = _position.collateralBitmap;
while (_collateralBitmap != 0) {
    uint256 i = UtilsLib.msb(_collateralBitmap);
    CollateralParams memory _collateralParam = market.collateralParams[i];
    uint256 price = IOracle(_collateralParam.oracle).price();   // ← reverts here
    ...
}
``` [3](#0-2) 

The protocol itself documents this in the LIVENESS section but provides no recovery mechanism:

> "If an activated collateral oracle reverts on price, liquidate reverts."
> "If an activated collateral oracle reverts on price, isHealthy, withdrawCollateral and take revert when the user (seller for take) has non-zero debt." [4](#0-3) 

**No deactivation escape hatch exists**

`supplyCollateral` explicitly notes the authorization check exists "to prevent activated collateral poisoning": [5](#0-4) 

Yet there is no corresponding function to *deactivate* a collateral slot without withdrawing it. The only way to clear a bit from `collateralBitmap` is to withdraw the full balance of that collateral: [6](#0-5) 

For a borrower with outstanding debt, `isHealthy` is called after the bitmap is cleared. If the remaining collateral is insufficient (or the borrower has only the poisoned collateral), `isHealthy` returns `false` and the withdrawal reverts — the borrower cannot clear the poisoned slot.

**`repay` is the only escape, but only for solvent borrowers**

`repay` does not call any oracle, so a borrower who can fully repay their debt can then withdraw collateral (since `isHealthy` skips oracle calls when `debt == 0`). However, an *underwater* borrower — the exact case that requires liquidation — cannot self-rescue: they lack the loan tokens to repay, and liquidators are also blocked. [7](#0-6) 

### Impact Explanation

For any borrower whose activated oracle begins reverting and who cannot self-repay:

1. **Collateral permanently locked** — `withdrawCollateral` reverts on every call.
2. **Liquidation permanently blocked** — `liquidate` reverts even with `seizedAssets = 0, repaidUnits = 0` (the bad-debt-only path), because the oracle loop runs unconditionally before the `seizedAssets/repaidUnits` branch.
3. **Bad debt never socialized** — `_marketState.lossFactor` is never updated; lenders' credit is never slashed to reflect the loss, yet the underlying loan tokens are unrecoverable.
4. **Market permanently degraded** — `totalUnits` remains inflated relative to actual recoverable value; lenders who try to `withdraw` receive less than their true share.

Severity: **Critical** — permanent, irrecoverable loss of lender funds with no admin override or governance escape.

### Likelihood Explanation

Oracle reversion is a realistic, non-theoretical event:

- Chainlink and other feed providers deprecate price feeds; deprecated feeds revert or stop updating.
- A market creator (permissionless in Midnight) can deploy a market with a malicious oracle that works initially and is later made to revert, trapping any borrowers who trusted the market.
- The `CollateralParams.oracle` field accepts any address with no validation at market creation time beyond the LLTV/maxLif checks. [8](#0-7) [9](#0-8) 

Markets are permissionless and immutable once created; there is no governance mechanism to swap an oracle after the fact.

### Recommendation

1. **Add a `deactivateCollateral` function** that clears a collateral slot from `collateralBitmap` without requiring a withdrawal, callable by the borrower (or an authorized account). Health should be checked using only the *remaining* activated collaterals, which already works correctly once the bit is cleared before `isHealthy` is called (as seen in `withdrawCollateral`).

2. **Skip reverting oracles in the bad-debt-only liquidation path** — when `seizedAssets == 0 && repaidUnits == 0`, the oracle values are not needed to compute any output; the loop could be made optional or the bad-debt path separated.

3. **Document a recovery procedure** analogous to the one recommended in the external report: pause the market, drain user balances, deactivate the poisoned collateral slot, and resume — mirroring the Aave/Morpho PR 569 approach.

### Proof of Concept

**Setup:**
- Market M with collateral token C, oracle O (initially functional), loan token L.
- Alice supplies 1000 C, takes a sell offer (borrows 800 L units). `collateralBitmap` bit 0 is set.
- Alice is now underwater: oracle O is deprecated/broken and begins reverting on `price()`.

**Step 1 — Alice cannot withdraw collateral:**
```
withdrawCollateral(M, 0, 1000, alice, alice)
  → isHealthy(M, id, alice)
    → IOracle(O).price()  ← REVERT
  ← REVERT (UnhealthyBorrower propagates the oracle revert)
```

**Step 2 — Liquidators cannot liquidate Alice:**
```
liquidate(M, 0, 0, 0, alice, false, liquidator, address(0), "")
  → while (collateralBitmap != 0):
      IOracle(O).price()  ← REVERT
  ← REVERT
```

Even the zero-value bad-debt-realization call reverts because the oracle loop is unconditional.

**Step 3 — Alice cannot borrow more (take as seller):**
```
take(offer_where_alice_is_seller, ...)
  → isHealthy(M, id, alice)  ← REVERT
```

**Result:** Alice's 1000 C is permanently locked in the contract. The 800 L of bad debt is never socialized. Lenders' `credit` values remain inflated while the backing assets are unrecoverable.

### Citations

**File:** src/Midnight.sol (L143-145)
```text
/// @dev If an activated collateral oracle reverts on price, liquidate reverts.
/// @dev If an activated collateral oracle reverts on price, isHealthy, withdrawCollateral and take revert when the user
/// (seller for take) has non-zero debt.
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

**File:** src/Midnight.sol (L523-527)
```text
    /// @dev This function checks authorization to prevent activated collateral poisoning.
    function supplyCollateral(Market memory market, uint256 collateralIndex, uint256 assets, address onBehalf)
        external
    {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L564-568)
```text
        if (newCollateral == 0 && assets > 0) {
            _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
        }

        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());
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

**File:** src/interfaces/IMidnight.sol (L14-19)
```text
struct CollateralParams {
    address token;
    uint256 lltv;
    uint256 maxLif;
    address oracle;
}
```
