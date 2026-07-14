### Title
Inconsistent Maturity Boundary Allows Debt Increase at `block.timestamp == maturity` While Post-Maturity Liquidation Requires Strictly After — (`src/Midnight.sol`)

---

### Summary

`take` permits a seller to increase debt when `block.timestamp == market.maturity` (uses `<=`), but `liquidate` in post-maturity mode requires `block.timestamp > market.maturity` (uses `>`). This is the same off-by-one boundary inconsistency as the referenced report. At the exact maturity block, a borrower can open or increase a debt position that is immediately past-due, yet cannot be post-maturity liquidated in that same block.

---

### Finding Description

**Root cause — two inconsistent operators on the same boundary:**

In `take` (line 391):
```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
`<=` means `block.timestamp == maturity` is **permitted** to increase debt. [1](#0-0) 

In `liquidate` (line 622):
```solidity
postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt
```
`>` means `block.timestamp == maturity` is **not** post-maturity mode — the condition is false, and the call reverts with `NotLiquidatable()`. [2](#0-1) 

**Exploit path at `block.timestamp == maturity`:**

1. Seller (borrower) calls `take` against a buy offer. `block.timestamp <= maturity` is true → `sellerDebtIncrease` is allowed → debt is written to the position.
2. In the same block, any liquidator calls `liquidate(..., postMaturityMode = true, ...)`. The check `block.timestamp > market.maturity` evaluates to `maturity > maturity` → **false** → reverts `NotLiquidatable()`.
3. Normal-mode liquidation (`postMaturityMode = false`) requires `originalDebt > maxDebt` (unhealthy). If the position is healthy (collateral covers debt), this also reverts.
4. Result: the borrower holds past-due debt that is unliquidatable for the entire duration of that block. [3](#0-2) 

---

### Impact Explanation

At exactly maturity, a borrower can take on new debt that is immediately past-due but cannot be post-maturity liquidated. If the position is healthy (collateral value ≥ debt), no liquidation path is available in that block. The lender's funds are locked in a state where the debt is due but the protocol's post-maturity enforcement mechanism is unavailable. In the next block, post-maturity liquidation becomes available with `lif = WAD` (no incentive), growing linearly to `maxLif` over `TIME_TO_MAX_LIF = 15 minutes`. [4](#0-3) 

The direct financial impact is a one-block window of unliquidatable past-due debt. This is less severe than the referenced report (permanent loss), but the inconsistency is structurally identical and violates the protocol's invariant that debt cannot be increased post-maturity.

---

### Likelihood Explanation

Any user can time a `take` call to land in the maturity block. On chains with predictable block times (e.g., Ethereum mainnet at 12s), this is straightforward. The scenario is not hypothetical — MEV bots and sophisticated users routinely target exact-block timing. Markets with high-value positions near maturity are natural targets.

---

### Recommendation

Make the two boundary checks consistent. The simplest fix is to change `take` to use strict inequality, matching `liquidate`'s post-maturity gate:

```solidity
// Before (line 391):
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());

// After:
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

This ensures that at `block.timestamp == maturity`, debt cannot be increased, which is consistent with post-maturity liquidation being available only when `block.timestamp > maturity`. [1](#0-0) 

---

### Proof of Concept

```
Setup:
  - Market with maturity = T
  - Borrower has sufficient collateral for a healthy position
  - A buy offer exists at tick X

At block.timestamp = T (maturity block):
  1. Borrower calls take(offer, ..., units=N, ...)
     → block.timestamp (T) <= offer.market.maturity (T) → true
     → sellerDebtIncrease = N is allowed
     → position[id][borrower].debt += N  ✓

  2. Liquidator calls liquidate(..., postMaturityMode=true, ...)
     → check: block.timestamp (T) > market.maturity (T) → false
     → revert NotLiquidatable()  ✗

  3. Liquidator calls liquidate(..., postMaturityMode=false, ...)
     → check: originalDebt > maxDebt
     → if position is healthy (collateral covers debt): false
     → revert NotLiquidatable()  ✗

Result: Borrower holds N units of past-due debt with no liquidation path
        for the entire maturity block.
``` [2](#0-1) [1](#0-0)

### Citations

**File:** src/Midnight.sol (L358-391)
```text
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
        uint256 _settlementFee = settlementFee(id, timeToMaturity);
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }

        (address buyer, address seller) = offer.buy ? (offer.maker, taker) : (taker, offer.maker);
        Position storage buyerPos = position[id][buyer];
        Position storage sellerPos = position[id][seller];

        if (hasCredit(id, buyer) || units > buyerPos.debt) _updatePosition(offer.market, id, buyer);
        if (hasCredit(id, seller)) _updatePosition(offer.market, id, seller);

        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
        uint128 buyerPendingFeeIncrease =
            UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
        uint128 sellerPendingFeeDecrease = sellerPos.credit > 0
            ? UtilsLib.toUint128(sellerPos.pendingFee.mulDivUp(sellerCreditDecrease, sellerPos.credit))
            : 0;

        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```

**File:** src/libraries/ConstantsLib.sol (L19-19)
```text
uint256 constant TIME_TO_MAX_LIF = 15 minutes;
```
