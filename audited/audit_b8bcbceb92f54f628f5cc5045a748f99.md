All code references check out. Let me verify the structural reachability of `offerPrice == settlementFee` given the constraints.

- `PRICE_ROUNDING_STEP = 1e12` — tick prices are multiples of `1e12` [1](#0-0) 
- `CBP = 1e12` — settlement fees are stored in cbp units, so `settlementFee` is always a multiple of `1e12` [2](#0-1) 
- Both quantities share the same granularity, making exact equality structurally reachable for any non-zero settlement fee within the tick range. [3](#0-2) 

All five sub-claims in the report are confirmed by the code:

1. `sellerPrice = 0` when `offer.buy == true` and `offerPrice == settlementFee` [4](#0-3) 
2. `mulDivUp(x, WAD, 0)` underflows at `(d - 1)` under checked arithmetic, confirmed by `testMulDivUpDivisionByZero` [5](#0-4) [6](#0-5) 
3. `Midnight.take()` uses `mulDivDown(sellerPrice, WAD)` where the denominator is `WAD`, not `sellerPrice` — no revert when `sellerPrice = 0` [7](#0-6) 
4. `sellerAssetsToUnits` is called outside the `try/catch` in `supplyCollateralAndSellWithAssetsTarget` [8](#0-7) 
5. The NatDoc invariant `"midnight reverts too"` is false for the equality case [9](#0-8) 

---

Audit Report

## Title
Division-by-Zero in `sellerAssetsToUnits` When `tickToPrice(tick) == settlementFee` Causes DoS of `supplyCollateralAndSellWithAssetsTarget` - (File: src/periphery/TakeAmountsLib.sol)

## Summary

In `sellerAssetsToUnits`, when `offer.buy == true` and `offerPrice == settlementFee`, `sellerPrice` is computed as zero and passed as the denominator `d` to `mulDivUp`. Because `mulDivUp` computes `(x * y + (d - 1)) / d`, the sub-expression `(d - 1)` underflows under Solidity 0.8 checked arithmetic and reverts. `Midnight.take()` does not revert in this same state — it computes `sellerAssets = units.mulDivDown(0, WAD) = 0` with `WAD` as the denominator — so the NatDoc invariant "midnight reverts too" is false for the equality case. Because `supplyCollateralAndSellWithAssetsTarget` calls `sellerAssetsToUnits` outside its `try/catch` block, any victim whose `takes[]` array includes such an offer has their entire bundler transaction reverted.

## Finding Description

**Root cause — `sellerAssetsToUnits` (`src/periphery/TakeAmountsLib.sol` lines 44–46):**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...
```

When `offer.buy == true` and `offerPrice == settlementFee`, `sellerPrice = 0`. `mulDivUp` (`src/libraries/UtilsLib.sol` line 35) is:

```solidity
return (x * y + (d - 1)) / d;
```

With `d = 0`, the sub-expression `(d - 1)` underflows under Solidity 0.8 checked arithmetic and reverts with an arithmetic error. This is confirmed by `testMulDivUpDivisionByZero` in `test/UtilsLibTest.sol` (lines 80–84), which explicitly expects `stdError.arithmeticError` (not `divisionError`) because the revert occurs at `(d - 1)`, not at the division.

**Why `Midnight.take()` does NOT revert in the same state (`src/Midnight.sol` lines 361–364):**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;  // = 0
uint256 buyerPrice  = sellerPrice + _settlementFee;                           // = settlementFee
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : ...;   // denominator = WAD, fine
uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : ...; // = 0 * units / WAD = 0, fine
```

`mulDivDown` is `(x * y) / d` where `d = WAD` (1e18), not `sellerPrice`. No revert occurs; `sellerAssets = 0` is returned normally.

**Exploit path — `supplyCollateralAndSellWithAssetsTarget` (`src/periphery/MidnightBundles.sol` lines 285–300):**

```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(   // ← OUTSIDE try/catch; reverts here
        MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
    ),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
);
try IMidnight(MIDNIGHT).take(...) { ... } catch {}  // never reached
```

The `sellerAssetsToUnits` call is not wrapped in the `try/catch`. Its revert propagates unconditionally to the caller.

**Existing protections reviewed and found insufficient:**
- The NatDoc comment at line 34 of `TakeAmountsLib.sol` states `"Reverts if offerPrice < settlementFee in case of a buy offer (midnight reverts too)"`. This is correct only for the strict inequality. For the equality case, `midnight.take()` succeeds while `sellerAssetsToUnits` reverts — the invariant is broken.
- `buyerAssetsToUnits` is unaffected because it divides by `buyerPrice = sellerPrice + settlementFee = settlementFee > 0`.
- There is no `require(sellerPrice > 0)` guard in `sellerAssetsToUnits`.
- The `try/catch` in `supplyCollateralAndSellWithAssetsTarget` covers only `IMidnight.take()`, not the preceding `sellerAssetsToUnits` call.

## Impact Explanation

Any call to `supplyCollateralAndSellWithAssetsTarget` that includes a buy offer at the settlement-fee price point in its `takes[]` array reverts entirely. This permanently blocks the sell-via-periphery path for affected takers for as long as the offer exists at that tick. The attacker bears only the gas cost of posting one buy offer; no capital is at risk. This constitutes a targeted, persistent denial of service of a core periphery function.

## Likelihood Explanation

Settlement fees are readable via the public `settlementFee(id, ttm)` view function. Settlement fees are stored in cbp units (`CBP = 1e12`), and tick prices are also multiples of `1e12` (via `PRICE_ROUNDING_STEP`), making exact equality structurally reachable for any non-zero settlement fee within the tick range. The attacker bears only gas cost with no capital at risk. The DoS persists until the offer is cancelled (by the attacker, who has no incentive to do so) or the settlement fee changes to a value with no matching tick. The attack is repeatable: if the fee changes, the attacker posts a new offer at the new matching tick.

## Recommendation

Add an explicit guard in `sellerAssetsToUnits` for the `sellerPrice == 0` case. Since `Midnight.take()` succeeds with `sellerAssets = 0` when `sellerPrice = 0`, the correct inverse is that no finite number of units yields a positive `targetSellerAssets` — return `type(uint256).max` to signal this, which will be capped by `takes[i].units` and `consumableUnits` in the bundler loop, effectively skipping the offer gracefully:

```solidity
if (offer.buy && sellerPrice == 0) return type(uint256).max;
```

Alternatively, wrap the entire `min(sellerAssetsToUnits(...), ...)` computation in a `try/catch` inside `supplyCollateralAndSellWithAssetsTarget`, consistent with the documented intent to skip offers that revert. Also update the NatDoc on `sellerAssetsToUnits` to accurately reflect that the equality case (`offerPrice == settlementFee`) causes a revert in the periphery but not in `Midnight.take()`.

## Proof of Concept

1. Deploy Midnight and MidnightBundles on a fork.
2. Read `settlementFee(id, ttm)` for a live market; call the result `F`.
3. Find tick `t` such that `tickToPrice(t) == F` (both are multiples of `1e12`; iterate ticks or use `priceToTick`).
4. Post a buy offer at tick `t` with any valid `maxUnits > 0`.
5. Construct a `takes[]` array containing this offer and call `supplyCollateralAndSellWithAssetsTarget`.
6. Observe the transaction reverts with `stdError.arithmeticError` at `sellerAssetsToUnits`, not inside the `try/catch`.
7. Confirm that calling `Midnight.take()` directly on the same offer with any `units > 0` succeeds and returns `sellerAssets = 0`.

### Citations

**File:** src/libraries/TickLib.sol (L8-8)
```text
uint256 constant PRICE_ROUNDING_STEP = 1e12;
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

**File:** src/libraries/ConstantsLib.sol (L10-10)
```text
uint256 constant CBP = 1e12;
```

**File:** src/periphery/TakeAmountsLib.sol (L32-35)
```text
    /// @dev Forward: sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD).
    /// @dev Assumes that id and offer.market match.
    /// @dev Reverts if offerPrice < settlementFee in case of a buy offer (midnight reverts too).
    /// @dev Returns a number of units for which take yields exactly targetSellerAssets (not necessarily the smallest).
```

**File:** src/periphery/TakeAmountsLib.sol (L44-46)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
        return
            offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : targetSellerAssets.mulDivDown(WAD, sellerPrice);
```

**File:** src/libraries/UtilsLib.sol (L34-36)
```text
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
```

**File:** test/UtilsLibTest.sol (L80-84)
```text
    function testMulDivUpDivisionByZero(uint256 x, uint256 y) public {
        // because there is d-1.
        vm.expectRevert(stdError.arithmeticError);
        this.mulDivUp(x, y, 0);
    }
```

**File:** src/Midnight.sol (L361-364)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/periphery/MidnightBundles.sol (L285-300)
```text
            uint256 unitsToTake = min(
                TakeAmountsLib.sellerAssetsToUnits(
                    MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
                ),
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
            try IMidnight(MIDNIGHT)
                .take(
                    takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(this), address(0), ""
                ) returns (
                uint256, uint256 resSellerAssets
            ) {
                filledUnits += unitsToTake;
                filledSellerAssets += resSellerAssets;
            } catch {}
```
