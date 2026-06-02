Looking at the actual code to validate each claim in the report.

**Claim 1: `mulDivDown(1, buyerPrice, WAD) = 0` for `buyerPrice < WAD`** [1](#0-0) 

`(1 * buyerPrice) / 1e18 = 0` for any `buyerPrice < 1e18`. Confirmed.

**Claim 2: `buyerAssets` is computed from `mulDivDown` for buy offers** [2](#0-1) 

For a buy offer with `units=1` and `tick < MAX_TICK`: `buyerAssets = 0`. Confirmed.

**Claim 3: Consumed accumulator increments by `buyerAssets` (= 0) for buy offers** [3](#0-2) 

`consumed += 0`, so `newConsumed` is unchanged and `require(newConsumed <= offer.maxAssets)` trivially passes every iteration. Confirmed.

**Claim 4: Credit/debt mutation uses `units`, not `buyerAssets`** [4](#0-3) 

`buyerCreditIncrease` and `sellerDebtIncrease` are derived from `units` (=1). `buyerPos.credit += 1`, `sellerPos.debt += 1` per call. Confirmed.

**Claim 5: Token transfer is 0** [5](#0-4) 

`buyerAssets - sellerAssets = 0 - 0 = 0`. No tokens move. Confirmed.

**Claim 6: NatSpec acknowledges the behavior without marking it as accepted risk** [6](#0-5) 

Line 94 explicitly states: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* This documents the behavior but does not mark it as an accepted security risk — it does not say "by design" or "accepted," and it does not bound the number of units that can be given.

**`totalUnits` inflation impact on `lossFactor`** [7](#0-6) [8](#0-7) 

`totalUnits` grows by 1 per call without a token deposit. This dilutes the `lossFactor` update during liquidations, amplifying losses for honest lenders.

---

All six claims check out against the actual code. The exploit path is fully reachable by an unprivileged user with two addresses and sufficient collateral for Account B. The impact (unbacked credit redeemable against other lenders' deposits) is concrete and in-scope.

---

Audit Report

## Title
Buy offer `maxAssets` cap bypassed via zero-rounding `buyerAssets` on sub-WAD price - (File: `src/Midnight.sol`)

## Summary
When a buy offer has `maxAssets > 0` and `tick < MAX_TICK` (i.e., `buyerPrice < WAD`), calling `take` with `units = 1` produces `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0` due to integer truncation. The `consumed` accumulator increments by zero, making the cap check a no-op every iteration. Because credit and debt deltas are computed from `units` (not `buyerAssets`), an attacker can mint unbacked lender credit and borrower debt indefinitely with zero token transfer per call.

## Finding Description

**Root cause — integer truncation in `mulDivDown`:**

`UtilsLib.mulDivDown` is `(x * y) / d` (plain integer division). For any `tick < MAX_TICK`, `buyerPrice < WAD = 1e18`. With `units = 1`: `(1 * buyerPrice) / 1e18 = 0`.

**Consumed accumulator does not advance:**

```solidity
// src/Midnight.sol lines 363, 367-369
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // = 0 when units=1, buyerPrice<WAD
    : units.mulDivUp(buyerPrice, WAD);

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy
        ? buyerAssets   // += 0 → consumed unchanged
        : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets()); // trivially passes
}
```

**Position mutation still executes normally:**

Credit and debt deltas are computed from `units` (= 1), not from `buyerAssets`:

```solidity
// src/Midnight.sol lines 382-414
uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt); // = 1
uint256 sellerDebtIncrease = units - sellerCreditDecrease;                 // = 1
...
buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease); // +1 per call
sellerPos.debt  += UtilsLib.toUint128(sellerDebtIncrease);  // +1 per call
```

The token transfer at line 455 sends `buyerAssets - sellerAssets = 0` tokens.

**Existing guard is insufficient:**

The sole cap guard is `require(newConsumed <= offer.maxAssets)`. When `buyerAssets = 0`, `newConsumed` equals its pre-call value and the check never rejects regardless of how many times `take` is called. The NatSpec at line 94 explicitly acknowledges this: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1"* — but does not mark it as an accepted security risk or bound the number of units that can be given.

**Exploit flow:**
1. Attacker (Account A) creates a buy offer with `maxAssets > 0` and any `tick < MAX_TICK`.
2. Attacker (Account B, separate address to satisfy `require(offer.maker != taker)`) calls `take(offer, ..., units=1, ...)` in a loop or via `multicall`.
3. Each iteration: Account A gains 1 credit unit, Account B gains 1 debt unit, 0 tokens transferred, `consumed` unchanged.
4. Account B supplies collateral to remain healthy.
5. `totalUnits` inflates by 1 per call without a corresponding token deposit, diluting the `lossFactor` update during future liquidations.
6. At maturity, Account A redeems excess unbacked credit against the pool, draining tokens deposited by other lenders.

## Impact Explanation
Account A accumulates credit representing a claim on loan tokens at maturity without a corresponding deposit. When redeemed via `withdraw`, the protocol pays out tokens sourced from other users' repayments — direct theft of funds. Simultaneously, `totalUnits` inflation distorts bad-debt socialization: the `lossFactor` update at liquidation divides by `totalUnits`, so inflated `totalUnits` dilutes loss propagation and amplifies losses for honest lenders. Severity: Critical.

## Likelihood Explanation
Preconditions are the normal operating mode: any buy offer with `maxAssets > 0` and any `tick < MAX_TICK` (every tick except the maximum) is vulnerable. No privileged access, oracle manipulation, or token quirks are required. The attacker needs only two addresses and enough collateral to keep Account B healthy. The attack is repeatable in a single transaction via `multicall` and applies to every market.

## Recommendation
Replace the `maxAssets` cap check with a `units`-based guard when `buyerPrice < WAD` would cause `buyerAssets` to round to zero, or enforce a minimum `units` threshold such that `mulDivDown(units, buyerPrice, WAD) >= 1`. Concretely, add a pre-check:

```solidity
if (offer.maxAssets > 0 && offer.buy) {
    require(buyerAssets > 0 || offer.maxAssets == 0, ZeroAssetsTake());
}
```

Alternatively, switch the cap tracking for buy offers to `units` when `buyerAssets == 0`, or require `buyerAssets >= 1` unconditionally when `maxAssets > 0`.

## Proof of Concept
1. Deploy a market with any `tick < MAX_TICK` (e.g., `tick = MAX_TICK - 4`), so `buyerPrice < WAD`.
2. Account A creates a buy offer: `maxAssets = 1000`, `maxUnits = 0`, `tick = MAX_TICK - 4`.
3. Account B calls `take(offer, ..., units=1, ...)` 1001 times (or via `multicall` in one tx).
4. Assert: `consumed[A][group] == 0` after all calls (cap never advanced).
5. Assert: `position[id][A].credit == 1001` (unbacked credit minted).
6. Assert: `position[id][B].debt == 1001` (debt minted with 0 tokens received).
7. Assert: token balance of contract unchanged (0 tokens transferred).
8. At maturity, Account A calls `withdraw` for 1001 units — drains tokens deposited by other lenders.

### Citations

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** src/Midnight.sol (L89-94)
```text
/// OFFER CAPS
/// @dev At most one of maxAssets or maxUnits can be nonzero per offer.
/// @dev maxAssets caps max buyer assets if offer.buy is true, and caps max seller assets otherwise.
/// @dev If maxAssets > 0, assets are capped to maxAssets, otherwise units are capped to maxUnits.
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L363-364)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/Midnight.sol (L367-373)
```text
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
```

**File:** src/Midnight.sol (L382-414)
```text
        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
        uint128 buyerPendingFeeIncrease =
            UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
        uint128 sellerPendingFeeDecrease = sellerPos.credit > 0
            ? UtilsLib.toUint128(sellerPos.pendingFee.mulDivUp(sellerCreditDecrease, sellerPos.credit))
            : 0;

        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
        require(
            !offer.reduceOnly || (offer.buy ? buyerCreditIncrease == 0 : sellerDebtIncrease == 0),
            MakerCreditOrDebtIncreased()
        );

        require(
            offer.market.enterGate == address(0) || buyerCreditIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseCredit(buyer),
            BuyerGatedFromIncreasingCredit()
        );
        require(
            offer.market.enterGate == address(0) || sellerDebtIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseDebt(seller),
            SellerGatedFromIncreasingDebt()
        );

        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/Midnight.sol (L416-417)
```text
        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/Midnight.sol (L629-634)
```text
            uint256 _totalUnits = _marketState.totalUnits;
            uint256 _lossFactor = _marketState.lossFactor;
            _marketState.lossFactor = UtilsLib.toUint128(
                type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
            );
            _marketState.totalUnits -= UtilsLib.toUint128(badDebt);
```
