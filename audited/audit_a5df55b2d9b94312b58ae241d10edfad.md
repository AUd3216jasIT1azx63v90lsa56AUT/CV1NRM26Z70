### Title
Zero settlement fee collected on sub-WAD takes due to `mulDivDown`/`mulDivUp` rounding — (File: src/Midnight.sol)

### Summary
In `take()`, `claimableSettlementFee` is incremented by `buyerAssets - sellerAssets`, where both quantities are computed via `mulDivDown` (buy offers) or `mulDivUp` (sell offers) with denominator `WAD`. When `units * _settlementFee < WAD`, integer rounding causes `buyerAssets - sellerAssets = 0` even though `_settlementFee > 0` and `units > 0`. No existing guard prevents this. The piecewise-linear interpolation in `settlementFee()` adds a secondary truncation that can further suppress the effective fee, but the `mulDivDown`/`mulDivUp` rounding alone is sufficient to trigger the invariant violation.

### Finding Description

**Code path — `settlementFee()` (line 979):**

```solidity
return (feeLower * (end - timeToMaturity) + feeUpper * (timeToMaturity - start)) / (end - start);
```

Integer division truncates. The minimum non-zero result is `1 * CBP = 1e12` (one centi-basis-point), because fees are stored in CBP units and multiplied by `CBP = 1e12`. [1](#0-0) [2](#0-1) 

**Code path — `take()` (lines 360–364, 418):**

```solidity
uint256 _settlementFee = settlementFee(id, timeToMaturity);          // line 360
uint256 sellerPrice    = offer.buy ? offerPrice - _settlementFee : offerPrice; // line 361
uint256 buyerPrice     = sellerPrice + _settlementFee;                // line 362
uint256 buyerAssets    = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // floor(units*buyerPrice/WAD)
    : units.mulDivUp(buyerPrice, WAD);    // ceil(units*buyerPrice/WAD)  // line 363
uint256 sellerAssets   = offer.buy
    ? units.mulDivDown(sellerPrice, WAD)
    : units.mulDivUp(sellerPrice, WAD);   // line 364
...
claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets; // line 418
``` [3](#0-2) [4](#0-3) 

**Root cause — buy offer case (`offer.buy = true`):**

`buyerAssets = floor(units * buyerPrice / WAD)` and `sellerAssets = floor(units * sellerPrice / WAD)`.

When `units * buyerPrice < WAD`, both floors are 0, so `buyerAssets - sellerAssets = 0`.

Concrete attacker-controlled inputs:
- `units = 1`
- `_settlementFee = 1e12` (1 CBP, minimum non-zero)
- `sellerPrice = 1e12` → `buyerPrice = 2e12`
- `floor(1 * 2e12 / 1e18) = 0`, `floor(1 * 1e12 / 1e18) = 0`
- `claimableSettlementFee` increases by **0**

This is reachable for any buy offer whose `offerPrice` is small (e.g., `offerPrice = 2e12`, which is a valid tick price). The taker chooses `units = 1`. [5](#0-4) 

**Root cause — sell offer case (`offer.buy = false`):**

`buyerAssets = ceil(units * buyerPrice / WAD)` and `sellerAssets = ceil(units * sellerPrice / WAD)`.

When `units * sellerPrice > 0` but both values fall in the same ceiling bucket (i.e., `units * _settlementFee` does not push the product past the next WAD boundary), both ceilings are equal and the fee is 0.

Concrete inputs:
- `units = 1`, `sellerPrice = 1` (1 wei), `_settlementFee = 1e12`
- `buyerPrice = 1 + 1e12`
- `ceil((1 + 1e12) / 1e18) = 1`, `ceil(1 / 1e18) = 1`
- `buyerAssets - sellerAssets = 0`

The taker pays 1 token, the maker receives 1 token, the protocol receives **0 fee**. [6](#0-5) 

**Interpolation compounding:**

The interpolation truncation at line 979 can reduce `_settlementFee` by up to `(end - start - 1)/(end - start)` of the fee-range width. For the 0–1 day segment this is up to ~14 CBP. A lower effective `_settlementFee` widens the set of `(units, sellerPrice)` pairs for which `units * _settlementFee < WAD`, but the mulDivDown/mulDivUp rounding alone is sufficient to trigger the violation. [7](#0-6) 

**Existing checks — insufficient:**

The only fee-adjacent guard is the implicit underflow revert when `offerPrice < _settlementFee` (line 361, Solidity 0.8 checked arithmetic). There is no check that `buyerAssets > sellerAssets` when `_settlementFee > 0`, and no minimum-units-per-take floor. [8](#0-7) 

### Impact Explanation

The protocol collects zero settlement fee on any take where `units * _settlementFee < WAD`. The maximum fee leakage per take is bounded by `WAD / WAD = 1` loan token (since the condition requires `units * _settlementFee < WAD`). A taker can repeatedly execute zero-fee takes, accumulating credit or reducing debt without contributing to `claimableSettlementFee`. The invariant "claimableSettlementFee must increase by at least 1 whenever `settlementFee(id, ttm) > 0` and `units > 0`" is concretely violated. The per-take loss is sub-token, but it is systematic and repeatable across all markets.

### Likelihood Explanation

The precondition `units * _settlementFee < WAD` is trivially satisfied with `units = 1` for any market with a non-zero settlement fee, since the minimum non-zero fee is `1 CBP = 1e12 << WAD = 1e18`. Any unprivileged taker can trigger this on any offer at any time. No special market state, oracle value, or privileged action is required. The condition is also satisfiable for larger `units` (up to `WAD / _settlementFee - 1`) when the taker selects an appropriate offer price.

### Recommendation

Add a minimum-fee enforcement in `take()` after computing `buyerAssets` and `sellerAssets`: if `_settlementFee > 0` and `units > 0`, require `buyerAssets > sellerAssets` (i.e., at least 1 wei of fee is collected). Alternatively, compute the fee amount directly as `units.mulDivUp(_settlementFee, WAD)` and add it to `claimableSettlementFee` separately, rather than deriving it as the difference of two independently rounded quantities. This ensures the fee is always rounded up when non-zero, consistent with the protocol's intent to collect fees on every non-trivial take.

### Proof of Concept

```solidity
// Foundry unit test — add to SettlementFeeTest.sol
function testZeroFeeOnSmallBuyTake() public {
    // Set minimum non-zero settlement fee: 1 CBP = 1e12
    midnight.setDefaultSettlementFee(address(loanToken), 1, 1e12);

    // Create market with TTM = 1 day (at breakpoint, no interpolation truncation)
    // sellerPrice = 1e12, buyerPrice = 2e12, both < WAD
    // Tick corresponding to offerPrice = 2e12 (buy offer: offerPrice = buyerPrice)
    // units = 1 → buyerAssets = floor(1 * 2e12 / 1e18) = 0
    //           sellerAssets = floor(1 * 1e12 / 1e18) = 0

    uint256 feeBefore = midnight.claimableSettlementFee(address(loanToken));
    // ... setup offer with offerPrice = 2e12, take with units = 1 ...
    uint256 feeAfter = midnight.claimableSettlementFee(address(loanToken));

    // Invariant: fee must increase when _settlementFee > 0 and units > 0
    assertGt(feeAfter, feeBefore, "claimableSettlementFee must increase");
    // This assertion FAILS: feeAfter == feeBefore == 0
}

function testZeroFeeOnSmallSellTake() public {
    midnight.setDefaultSettlementFee(address(loanToken), 1, 1e12);
    // sellerPrice = 1 wei, buyerPrice = 1 + 1e12
    // units = 1 → buyerAssets = ceil((1+1e12)/1e18) = 1
    //           sellerAssets = ceil(1/1e18) = 1
    // fee = 0

    uint256 feeBefore = midnight.claimableSettlementFee(address(loanToken));
    // ... setup sell offer with offerPrice tick → sellerPrice = 1, take with units = 1 ...
    uint256 feeAfter = midnight.claimableSettlementFee(address(loanToken));

    assertGt(feeAfter, feeBefore, "claimableSettlementFee must increase");
    // This assertion FAILS
}
```

Expected: both assertions fail, confirming the invariant violation. A fuzz test over `(units, feeLower, feeUpper, ttm, sellerPrice)` with the assertion `if (_settlementFee > 0 && units > 0) assert(feeAfter > feeBefore)` will find counterexamples immediately at `units = 1`.

### Citations

**File:** src/Midnight.sol (L360-364)
```text
        uint256 _settlementFee = settlementFee(id, timeToMaturity);
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/Midnight.sol (L418-418)
```text
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

**File:** src/Midnight.sol (L970-979)
```text
        (uint256 start, uint256 end, uint256 feeLower, uint256 feeUpper) =
            timeToMaturity < 1 days   ? (  0 days,   1 days, _marketState.settlementFeeCbp0 * CBP, _marketState.settlementFeeCbp1 * CBP) :
            timeToMaturity < 7 days   ? (  1 days,   7 days, _marketState.settlementFeeCbp1 * CBP, _marketState.settlementFeeCbp2 * CBP) :
            timeToMaturity < 30 days  ? (  7 days,  30 days, _marketState.settlementFeeCbp2 * CBP, _marketState.settlementFeeCbp3 * CBP) :
            timeToMaturity < 90 days  ? ( 30 days,  90 days, _marketState.settlementFeeCbp3 * CBP, _marketState.settlementFeeCbp4 * CBP) :
            timeToMaturity < 180 days ? ( 90 days, 180 days, _marketState.settlementFeeCbp4 * CBP, _marketState.settlementFeeCbp5 * CBP) :
                                        (180 days, 360 days, _marketState.settlementFeeCbp5 * CBP, _marketState.settlementFeeCbp6 * CBP);
        // forgefmt: disable-end

        return (feeLower * (end - timeToMaturity) + feeUpper * (timeToMaturity - start)) / (end - start);
```

**File:** src/libraries/ConstantsLib.sol (L10-10)
```text
uint256 constant CBP = 1e12;
```

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** src/libraries/UtilsLib.sol (L34-36)
```text
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
```
