### Title
`buyerPendingFeeIncrease` Truncates to Zero for Dust Fills When `units * continuousFee * timeToMaturity < WAD` - (`src/Midnight.sol`)

### Summary

In `Midnight.take`, `buyerPendingFeeIncrease` is computed as `buyerCreditIncrease.mulDivDown(continuousFee * timeToMaturity, WAD)`. When `units * continuousFee * timeToMaturity < WAD`, integer floor division truncates the result to zero, so the buyer's `pendingFee` is not incremented despite receiving real credit. Repeating fills with `units ≤ floor(WAD / (continuousFee * timeToMaturity)) - 1` accumulates credit with no continuous fee obligation, violating the invariant that every credit increase with `continuousFee > 0` and `TTM > 0` must carry a proportional `pendingFee`.

### Finding Description

**Root cause — line 385–386 of `src/Midnight.sol`:**

```solidity
uint128 buyerPendingFeeIncrease =
    UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
```

`mulDivDown` is plain integer division: `(x * y) / d` with no rounding-up guard. [1](#0-0) 

`MAX_CONTINUOUS_FEE = uint32(0.01e18 / 365 days) ≈ 317,097,919`. [2](#0-1) 

**Truncation condition:** `units * continuousFee * timeToMaturity < WAD = 1e18`.

Concrete examples:
- `continuousFee = MAX_CONTINUOUS_FEE`, `timeToMaturity = 3153 s` (~52 min): `317097919 × 3153 = 999,999,666,807 < 1e18` → `mulDivDown(1, 999999666807, 1e18) = 0`.
- `continuousFee = MAX_CONTINUOUS_FEE`, `timeToMaturity = 1 s`: max zero-fee fill size = `floor(1e18 / 317097919) - 1 = 3152` units per call.

**Exploit flow:**

1. Maker creates a buy offer on a market with `continuousFee > 0` and short TTM (or low fee), calls `setIsRootRatified` to ratify it.
2. Taker calls `take(offer, ratifierData, units=K, ...)` where `K * continuousFee * timeToMaturity < WAD`.
3. `buyerCreditIncrease = K` (assuming no pre-existing debt), `buyerPendingFeeIncrease = 0`.
4. `buyerPos.credit += K`, `buyerPos.pendingFee += 0`.
5. Repeat N times (using a single offer with `maxUnits = N*K` or multiple offers): buyer accumulates `N*K` units of credit with `pendingFee = 0`.
6. When `_updatePosition` is later called, `fee = pendingFee.mulDivDown(elapsed, ttm) = 0`, so no credit is ever deducted. [3](#0-2) [4](#0-3) 

**Why existing checks do not stop it:**

- No minimum `units` check exists in `take`.
- No `require(buyerPendingFeeIncrease > 0)` guard.
- The Certora spec `continuousFeeNotOverchargedForBuyer` explicitly models the increase as `floor(creditDelta * contFee * timeToMaturity / WAD)` — it only bounds the upper side, not the lower side. [5](#0-4) 
- The invariant `pendingContinuousFeeBoundedByCredit` only enforces `pendingFee ≤ credit`, not `pendingFee ≥ minExpectedFee`. [6](#0-5) 

### Impact Explanation

A buyer accumulates credit with `pendingFee = 0`. Since fee accrual in `_updatePosition` is `pendingFee.mulDivDown(elapsed, ttm)`, zero `pendingFee` means zero credit deduction over the entire remaining TTM. The fee evaded per fill is up to `floor(WAD / (continuousFee * timeToMaturity)) - 1` units × `continuousFee * timeToMaturity / WAD` ≈ just under 1 unit per fill. Across many fills this is a systematic, repeatable continuous-fee evasion: the buyer holds credit that should carry a fee obligation but does not.

### Likelihood Explanation

Preconditions are reachable without any privileged action by the taker:
- Any market with `continuousFee > 0` and `timeToMaturity < WAD / continuousFee` (e.g., TTM < 52 minutes at max fee, or TTM < 3.65 days at 0.1% of max fee).
- Any offer with `maxUnits` large enough to allow repeated small fills.
- The taker only needs to call `take` with a small `units` value; no admin access is required.

The condition becomes easier to satisfy as a market approaches maturity, making it exploitable in the final hours/minutes of any market with a non-zero continuous fee.

### Recommendation

Replace `mulDivDown` with `mulDivUp` for `buyerPendingFeeIncrease`, or add a guard that enforces a minimum of 1 unit of `pendingFee` whenever `buyerCreditIncrease > 0` and `continuousFee > 0` and `timeToMaturity > 0`:

```solidity
uint128 buyerPendingFeeIncrease = buyerCreditIncrease > 0 && _marketState.continuousFee > 0 && timeToMaturity > 0
    ? UtilsLib.toUint128(buyerCreditIncrease.mulDivUp(_marketState.continuousFee * timeToMaturity, WAD))
    : 0;
```

Using `mulDivUp` ensures that any non-zero fee obligation results in at least 1 unit of `pendingFee`, closing the truncation-to-zero path. The Certora spec `continuousFeeNotOverchargedForBuyer` would need to be updated to reflect ceiling rounding.

### Proof of Concept

```solidity
// Foundry unit test
function testDustFillFeeEvasion() public {
    uint256 continuousFee = MAX_CONTINUOUS_FEE; // 317097919
    uint256 ttm = 3153; // seconds; continuousFee * ttm = 999999666807 < 1e18
    market.maturity = block.timestamp + ttm;
    id = toId(market);
    midnight.setDefaultContinuousFee(address(loanToken), continuousFee);

    uint256 unitsPerFill = 1; // 1 * 999999666807 < 1e18 → pendingFee = 0
    uint256 N = 1000;

    // Setup: borrower posts collateral, maker creates buy offer with maxUnits = N
    collateralize(market, borrower, N * 2);
    // ... create and ratify buy offer with maxUnits = N ...

    for (uint256 i = 0; i < N; i++) {
        take(unitsPerFill, taker, buyOffer);
    }

    // Assert: taker has N units of credit but zero pendingFee
    assertEq(midnight.creditOf(id, taker), N);
    assertEq(midnight.pendingFee(id, taker), 0); // INVARIANT VIOLATED

    // Assert: after full TTM elapses, no fee is deducted
    vm.warp(market.maturity);
    midnight.updatePosition(market, taker);
    assertEq(midnight.creditOf(id, taker), N); // credit unchanged — fee evaded
}
```

Expected assertions: `pendingFee(id, taker) == 0` after N fills, and `creditOf(id, taker) == N` after maturity with no deduction. A fuzz variant should assert `pendingFee > 0` for all `units ∈ [1, WAD / continuousFee]` when `continuousFee > 0 && timeToMaturity > 0`.

### Citations

**File:** src/libraries/UtilsLib.sol (L28-31)
```text
    /// @dev Returns (x * y) / d rounded down.
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** src/libraries/ConstantsLib.sol (L18-18)
```text
uint32 constant MAX_CONTINUOUS_FEE = uint32(uint256(0.01e18) / uint256(365 days));
```

**File:** src/Midnight.sol (L385-410)
```text
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
```

**File:** src/Midnight.sol (L813-818)
```text
        // forge-lint: disable-next-item(unsafe-typecast) as fee <= pending <= credit which are uint128 position fields
        uint128 fee = _lastAccrual < market.maturity
            ? uint128(postSlashPendingFee.mulDivDown(accrualEnd - _lastAccrual, market.maturity - _lastAccrual))
            : 0;
        // forge-lint: disable-next-item(unsafe-typecast) as credit and pending are <= uint128 position fields
        return (uint128(postSlashCredit) - fee, uint128(postSlashPendingFee) - fee, fee);
```

**File:** certora/specs/ContinuousFee.spec (L41-63)
```text
// The buyer's pendingFee increases by floor(creditIncrease * continuousFee * timeToMaturity / WAD).
rule continuousFeeNotOverchargedForBuyer(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    address buyer = offer.buy ? offer.maker : taker;

    bytes32 id;
    uint128 postUpdateCredit;
    uint128 postUpdatePendingFee;

    postUpdateCredit, postUpdatePendingFee, _ = updatePositionView(e, offer.market, id, buyer);

    require pendingFee(id, buyer) <= creditOf(id, buyer), "See pendingContinuousFeeBoundedByCredit in Midnight.spec";

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    require id == lastId, "id should be derived from market";

    uint256 contFee = continuousFee(id);
    uint256 timeToMaturity = e.block.timestamp <= offer.market.maturity ? assert_uint256(offer.market.maturity - e.block.timestamp) : 0;

    mathint creditDelta = creditOf(id, buyer) - postUpdateCredit;

    assert pendingFee(id, buyer) == postUpdatePendingFee + (creditDelta * contFee * timeToMaturity) / WAD();
}
```

**File:** certora/specs/Midnight.spec (L137-149)
```text
strong invariant pendingContinuousFeeBoundedByCredit(bytes32 id, address user)
    pendingFee(id, user) <= creditOf(id, user)
    {
        preserved with (env e) {
            requireInvariant continuousFeeBounded(id);
            requireInvariant defaultContinuousFeeBoundedAll();
        }
        preserved take(Midnight.Offer offer, bytes ratifierData, uint256 unitsInput, address taker, address receiverIfTakerIsSeller, address takerCallbackAddress, bytes takerCallbackData) with (env e) {
            requireInvariant continuousFeeBounded(id);
            requireInvariant defaultContinuousFeeBoundedAll();
            require to_mathint(offer.market.maturity) <= to_mathint(e.block.timestamp) + MAX_TTM(); // TODO verify this cleanly
        }
    }
```
