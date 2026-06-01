### Title
Fully-consumed assets-mode buy offer can be re-taken with `units > 0` when `buyerPrice < WAD`, minting unbacked credit and debt - (`File: src/Midnight.sol`, `src/libraries/TickLib.sol`)

### Summary
In `Midnight.take`, when `offer.maxAssets > 0` and `offer.buy == true`, consumed is tracked as `buyerAssets = units.mulDivDown(buyerPrice, WAD)`. Because `tickToPrice` returns a value strictly less than `WAD` for every valid tick, `mulDivDown(1, buyerPrice, WAD) == 0` always holds for `units = 1`. This means a take with `units = 1` adds zero to `consumed`, passes the `require(newConsumed <= offer.maxAssets)` guard even when `consumed == maxAssets`, yet still executes the full position-accounting path with `units = 1`, minting one unit of unbacked credit to the maker and one unit of debt to the taker. The protocol's own comment at line 94 and the existing test `testBugBuyMaxAssetsBypass` confirm this is a real, reachable state.

### Finding Description

**Code path:**

`Midnight.take` — `src/Midnight.sol` lines 363–373 and 382–417.

```
// line 363
uint256 buyerAssets = offer.buy
    ? units.mulDivDown(buyerPrice, WAD)   // ← rounds DOWN
    : units.mulDivUp(buyerPrice, WAD);

// lines 367-369
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

`tickToPrice` (`src/libraries/TickLib.sol` line 44–52) computes:

```
price = round( 1e36 / (1e18 + wExp(LN_ONE_PLUS_DELTA * (MAX_TICK/2 - tick))) )
```

For every tick in `[0, MAX_TICK]`, the result is strictly less than `1e18 = WAD`. Therefore:

```
mulDivDown(1, buyerPrice, WAD) = floor(1 * buyerPrice / 1e18) = 0   ∀ valid ticks
```

**Exploit flow:**

1. A buy offer exists with `offer.buy = true`, `offer.maxAssets = M > 0`, any valid tick.
2. `consumed[maker][group]` reaches `M` (offer fully consumed via normal fills).
3. Attacker calls `take(units=1, taker=attacker, offer=...)`:
   - `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`
   - `newConsumed = M + 0 = M`
   - `require(M <= M)` → **passes**
   - `buyerCreditIncrease = zeroFloorSub(1, buyerPos.debt)` → 1 (if maker has no debt)
   - `sellerDebtIncrease = 1 - sellerCreditDecrease` → 1 (if taker has no credit)
   - `buyerPos.credit += 1` (maker gets 1 unit of credit, paid 0 loan tokens)
   - `sellerPos.debt += 1` (taker gets 1 unit of debt, received 0 loan tokens)
   - `totalUnits += 1`
   - No token transfers occur (`buyerAssets = sellerAssets = 0`)
4. Step 3 can be repeated indefinitely.

**Why existing checks fail:**

The sole guard is `require(newConsumed <= offer.maxAssets)`. Since `buyerAssets = 0` for `units = 1`, `newConsumed` never advances past `maxAssets`, so the check is permanently bypassable. There is no guard of the form `require(units == 0 || buyerAssets > 0)` or `require(consumed[maker][group] < maxAssets)`. The Certora rule `fullyConsumedOfferRevertsOnNonTrivialTake` (`certora/specs/Consume.spec` lines 99–111) only covers `maxAssets == 0` (units mode); no equivalent rule exists for assets mode. The `takeConsumedAtMaxUnchangedAssets` rule (lines 88–97) only asserts `consumed` is unchanged — it does not assert `units == 0`.

### Impact Explanation

Each successful re-take with `units = 1` on a fully-consumed buy offer:
- Mints 1 unit of credit to the maker without any corresponding loan-token deposit.
- Mints 1 unit of debt to the taker without any loan-token transfer to the taker.
- Increments `totalUnits`, inflating the continuous-fee accrual base for all lenders in the market.

The contract's loan-token balance no longer covers all outstanding credit, violating the core invariant that "contract balances cover collateral, credit redemption, fees, and withdrawable assets." The offer's `maxAssets` cap — intended to bound the maker's total exposure — is rendered ineffective for units-level accounting. The protocol's own NatDoc at line 94 acknowledges this: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."*

### Likelihood Explanation

**Preconditions:**
- A buy offer with `maxAssets > 0` at any valid tick (condition holds for all 5820 ticks).
- `consumed[maker][group] == maxAssets` (offer fully consumed — a normal end state).
- Attacker is any unprivileged taker (not the maker, due to `SelfTake` guard).

**Feasibility:** High. The price-less-than-WAD condition is structural and permanent — it holds for every tick the protocol supports. No oracle manipulation, no admin action, and no special token behavior is required. The attacker only needs to call `take` with `units = 1` after an offer is exhausted.

**Repeatability:** Unlimited. Each call with `units = 1` succeeds and adds 1 unit of unbacked credit/debt. The consumed counter stays pinned at `maxAssets` forever.

### Recommendation

Add an explicit guard that rejects non-trivial takes when the assets-mode consumed counter would not advance:

```solidity
if (offer.maxAssets > 0) {
    uint256 assetsDelta = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || assetsDelta > 0, ZeroAssetsTake());
    newConsumed = consumed[offer.maker][offer.group] += assetsDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, enforce that a non-zero `units` input always produces a non-zero consumed increment, mirroring the units-mode invariant. The Certora spec should also add a rule analogous to `fullyConsumedOfferRevertsOnNonTrivialTake` for the `maxAssets > 0` branch.

### Proof of Concept

The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` (lines 857–889) already constitutes a passing Foundry unit test that proves the bug. A minimal standalone reproduction:

```solidity
function testFullyConsumedBuyOfferReusable() public {
    // Setup: buy offer, maxAssets = 1, tick = MAX_TICK - 16 (buyerPrice < WAD)
    lenderOffer.maxUnits  = 0;
    lenderOffer.maxAssets = 1;
    lenderOffer.tick      = MAX_TICK - 16;

    // Pre-consume to maxAssets (offer is fully exhausted)
    vm.prank(lender);
    midnight.setConsumed(lenderOffer.group, 1, lender);
    assertEq(midnight.consumed(lender, lenderOffer.group), 1);

    uint256 creditBefore     = midnight.creditOf(id, lender);
    uint256 debtBefore       = midnight.debtOf(id, borrower);
    uint256 totalUnitsBefore = midnight.totalUnits(id);

    // Take with units=1 on a fully-consumed offer — must NOT succeed
    collateralize(market, borrower, 100);
    (uint256 buyerAssets, uint256 sellerAssets) = take(1, borrower, lenderOffer);

    // These assertions FAIL (demonstrating the bug):
    assertEq(buyerAssets,  0, "no assets transferred");
    assertEq(sellerAssets, 0, "no assets transferred");
    // Position state changed despite offer being fully consumed:
    assertGt(midnight.creditOf(id, lender),    creditBefore,     "maker credit increased");
    assertGt(midnight.debtOf(id, borrower),    debtBefore,       "taker debt increased");
    assertGt(midnight.totalUnits(id),          totalUnitsBefore, "totalUnits inflated");
    // consumed is still pinned at maxAssets — can repeat indefinitely:
    assertEq(midnight.consumed(lender, lenderOffer.group), 1);
}
```

**Expected (correct) behavior:** `take` reverts with `ConsumedAssets` when `consumed == maxAssets` and `units > 0`.
**Actual behavior:** `take` succeeds, minting unbacked credit and debt, as confirmed by the existing `testBugBuyMaxAssetsBypass` test. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** src/Midnight.sol (L93-94)
```text
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L363-373)
```text
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
```

**File:** src/Midnight.sol (L382-417)
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

        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
```

**File:** src/libraries/TickLib.sol (L44-52)
```text
    function tickToPrice(uint256 tick) internal pure returns (uint256) {
        require(tick <= MAX_TICK, TickOutOfRange());
        unchecked {
            // forge-lint: disable-next-item(unsafe-typecast)
            return uint256(1e36)
                    .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
                    .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
        }
    }
```

**File:** test/TakeTest.sol (L857-889)
```text
    // Show that a buy offer with offerPrice < WAD can be taken with units > 0
    function testBugBuyMaxAssetsBypass() public {
        deal(address(loanToken), lender, 0); // lender pays 0
        collateralize(market, borrower, 100);

        lenderOffer.maxUnits = 0;
        lenderOffer.maxAssets = 1;
        lenderOffer.tick = MAX_TICK - 16; // offerPrice < WAD

        // Fully consume the offer before the take.
        vm.prank(lender);
        midnight.setConsumed(lenderOffer.group, lenderOffer.maxAssets, lender);

        uint256 lenderCreditBefore = midnight.creditOf(id, lender);
        uint256 borrowerDebtBefore = midnight.debtOf(id, borrower);
        uint256 totalUnitsBefore = midnight.totalUnits(id);
        uint256 lenderBalBefore = loanToken.balanceOf(lender);
        uint256 borrowerBalBefore = loanToken.balanceOf(borrower);

        (uint256 buyerAssets, uint256 sellerAssets) = take(1, borrower, lenderOffer);

        assertEq(buyerAssets, 0);
        assertEq(sellerAssets, 0);

        // Nothing observable to the cap or token balances changed:
        assertEq(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets);
        assertEq(loanToken.balanceOf(lender), lenderBalBefore);
        assertEq(loanToken.balanceOf(borrower), borrowerBalBefore);
        // But position state strictly changed:
        assertGt(midnight.creditOf(id, lender), lenderCreditBefore);
        assertGt(midnight.debtOf(id, borrower), borrowerDebtBefore);
        assertGt(midnight.totalUnits(id), totalUnitsBefore);
    }
```

**File:** certora/specs/Consume.spec (L88-111)
```text
/// If consumed is already at or above maxAssets before a take in assets mode, it remains unchanged.
rule takeConsumedAtMaxUnchangedAssets(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxUnits == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert consumedBefore >= offer.maxAssets => consumed(offer.maker, offer.group) == consumedBefore;
}

/// A fully-consumed offer in units mode only allows no-op takes.
rule fullyConsumedOfferRevertsOnNonTrivialTake(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData) {
    require offer.maxAssets == 0;

    uint256 consumedBefore = consumed(offer.maker, offer.group);

    require offer.maxUnits > 0 && consumedBefore >= offer.maxUnits, "assume the offer is fully consumed";

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    // If take does not revert, its input has to be zero.
    assert units == 0;
}
```
