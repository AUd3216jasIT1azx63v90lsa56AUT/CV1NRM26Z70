### Title
Settlement Fee Truncates to Zero for Small-Unit Buy-Offer Takes — (File: src/Midnight.sol)

### Summary
In `Midnight.sol`'s `take()` function, the settlement fee credited to the protocol (`claimableSettlementFee`) is computed as `buyerAssets - sellerAssets`. For buy offers, both values are computed with independent `mulDivDown` (floor) operations. When `units * _settlementFee < WAD`, both floor to the same integer and the difference is zero — the protocol collects no fee. This is the direct analog of the `grant.value / 100` truncation in the external report.

### Finding Description

In `take()`, the fee-bearing asset amounts are computed as: [1](#0-0) 

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

The protocol fee is then accumulated as: [2](#0-1) 

```solidity
claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

For a **buy offer** (`offer.buy = true`), both `buyerAssets` and `sellerAssets` use `mulDivDown` (floor division). Because they are computed independently, when `units * _settlementFee < WAD`, both expressions floor to the same integer and `buyerAssets - sellerAssets = 0`.

**Concrete example** (minimum non-zero fee, 1 CBP = `1e12`):
- `units = 1`, `sellerPrice = WAD = 1e18`, `_settlementFee = 1e12`
- `buyerPrice = 1e18 + 1e12`
- `buyerAssets = floor(1 × (1e18 + 1e12) / 1e18) = 1`
- `sellerAssets = floor(1 × 1e18 / 1e18) = 1`
- **fee collected = 0**

The zero-fee condition is `units < WAD / _settlementFee`. At the **maximum** settlement fee of `0.005e18 = 5e15` (360-day breakpoint): [3](#0-2) 

the threshold is `units < 1e18 / 5e15 = 200`. Any buy-offer take with fewer than 200 units at maximum fee pays zero settlement fee.

The `settlementFee()` interpolation itself: [4](#0-3) 

```solidity
return (feeLower * (end - timeToMaturity) + feeUpper * (timeToMaturity - start)) / (end - start);
```

does not truncate to zero (minimum non-zero result ≈ 64 for the 180d–360d range), so the root cause is exclusively the

### Citations

**File:** src/Midnight.sol (L363-364)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/Midnight.sol (L418-418)
```text
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

**File:** src/Midnight.sol (L979-979)
```text
        return (feeLower * (end - timeToMaturity) + feeUpper * (timeToMaturity - start)) / (end - start);
```

**File:** src/libraries/ConstantsLib.sol (L17-17)
```text
uint256 constant MAX_SETTLEMENT_FEE_360_DAYS = 0.005e18;
```
