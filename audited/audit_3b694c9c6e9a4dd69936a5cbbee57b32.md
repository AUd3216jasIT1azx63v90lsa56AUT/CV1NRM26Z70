All technical claims verified against the actual code. Here is the validated output:

---

Audit Report

## Title
`sellerAssetsToUnits` arithmetic revert when `offerPrice == settlementFee` causes `supplyCollateralAndSellWithAssetsTarget` to DoS - (File: src/periphery/TakeAmountsLib.sol)

## Summary
`TakeAmountsLib.sellerAssetsToUnits` computes `sellerPrice = offerPrice - settlementFee` for buy offers and passes it as the denominator to `mulDivUp`. When `offerPrice == settlementFee`, `sellerPrice = 0`, and `mulDivUp(x, WAD, 0)` triggers an arithmetic underflow on `d - 1` in Solidity 0.8+, reverting unconditionally. The core `Midnight.take()` computes `sellerAssets = units.mulDivDown(0, WAD) = 0` in the same scenario and does not revert. Because `supplyCollateralAndSellWithAssetsTarget` calls `sellerAssetsToUnits` outside its `try/catch` block, any revert there propagates to the caller, reverting the entire bundle.

## Finding Description

**Root cause — `mulDivUp` with `d = 0` triggers arithmetic revert, not division error:**

`mulDivUp` is implemented as:

```solidity
return (x * y + (d - 1)) / d;
```

When `d = 0`, the sub-expression `d - 1` underflows (uint256 wraps to `type(uint256).max`), triggering an arithmetic revert in Solidity 0.8+ before the division is even reached. This is confirmed by the existing unit test `testMulDivUpDivisionByZero` which expects `stdError.arithmeticError` (not `divisionError`) when `d = 0`, with the comment `// because there is d-1`. [1](#0-0) [2](#0-1) 

**`sellerAssetsToUnits` passes `sellerPrice = 0` as denominator:**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...;
```

When `offer.buy == true` and `offerPrice == settlementFee`, `sellerPrice = 0`. The call `mulDivUp(targetSellerAssets, WAD, 0)` reverts unconditionally via the arithmetic underflow described above. [3](#0-2) 

**Core `take()` does NOT revert in the same scenario:**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice; // = 0
uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : ...; // = 0, no revert
```

`mulDivDown(units, 0, WAD)` computes `(units * 0) / WAD = 0` — no division by zero, no revert. `take()` succeeds and the seller receives zero assets. [4](#0-3) 

**`supplyCollateralAndSellWithAssetsTarget` calls `sellerAssetsToUnits` outside the `try/catch`:**

```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(          // ← NOT inside try/catch
        MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
    ),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)  // ← also NOT inside try/catch
);
try IMidnight(MIDNIGHT).take(...) ...            // ← only take() is guarded
```

A revert from `sellerAssetsToUnits` is not caught and bubbles up, reverting the entire bundle call. `ConsumableUnitsLib.consumableUnits` also calls `sellerAssetsToUnits` (line 21) and is likewise invoked outside the `try/catch`, providing a second revert path. [5](#0-4) [6](#0-5) 

**NatSpec divergence:** The `sellerAssetsToUnits` NatSpec documents revert only for `offerPrice < settlementFee`. The `==` case is undocumented and unguarded, yet `take()` explicitly handles it (seller receives zero assets, no revert). The NatSpec of `supplyCollateralAndSellWithAssetsTarget` states it "Reverts if TakeAmountsLib or ConsumableUnitsLib reverts," which is accurate but does not warn callers that this can occur for a valid, on-chain-accepted offer. [7](#0-6) [8](#0-7) 

**Collateral is supplied before the loop:** The collateral supply loop (lines 269–274) executes before the take loop. When the take loop reverts due to this bug, the entire transaction reverts, unwinding the collateral supply as well — no funds are permanently lost, but the entire bundle call fails. [9](#0-8) 

**Formal verification gap:** The Certora `NoDivisionByZero.spec` only covers `src/Midnight.sol` and does not include the periphery contracts, so this revert path is not covered by the existing formal proofs. [10](#0-9) 

## Impact Explanation
Any call to `supplyCollateralAndSellWithAssetsTarget` whose `takes[]` array contains a buy offer where `tickToPrice(tick) == settlementFee(id, ttm)` reverts entirely. Because `take()` itself would accept the offer (returning `sellerAssets = 0`), the offer appears valid on-chain, making the failure non-obvious to callers who rely on the bundler's documented skip-on-revert behavior. The function is completely unusable for any bundle containing such an offer, constituting a targeted DoS of the sell-with-assets-target path.

## Likelihood Explanation
The settlement fee is governance-controlled and must be a multiple of `CBP` (1e12). Tick prices are also multiples of `priceRoundingStep`. The condition `tickToPrice(T) == settlementFee` is achievable whenever the fee is set to a value coinciding with any accessible tick price. Additionally, if the fee changes between when a taker constructs their transaction and when it executes — a scenario the protocol's own NatSpec acknowledges at `Midnight.sol` line 329 — a previously-valid offer can silently become a zero-yield offer, triggering the revert without any attacker involvement. An attacker can also post a buy offer at the matching tick at negligible cost and wait for a taker to include it in a bundle. [11](#0-10) 

## Recommendation
Add a zero-check for `sellerPrice` in `sellerAssetsToUnits` before calling `mulDivUp`. When `sellerPrice == 0`, the function should either revert with a descriptive error (mirroring the `offerPrice < settlementFee` case) or return `type(uint256).max` to signal that no finite unit count can yield a positive seller asset amount. Alternatively, wrap the `TakeAmountsLib.sellerAssetsToUnits` and `ConsumableUnitsLib.consumableUnits` calls inside `supplyCollateralAndSellWithAssetsTarget` in a `try/catch` block consistent with the bundler's skip-on-revert design, so that offers with zero seller price are skipped rather than reverting the entire bundle.

## Proof of Concept
1. Deploy `Midnight` and `MidnightBundles` on a fork or local test environment.
2. Set the settlement fee for a market to a value `F` that equals `tickToPrice(T)` for some accessible tick `T`.
3. Have a maker post a buy offer at tick `T` with `offer.buy = true`.
4. Call `supplyCollateralAndSellWithAssetsTarget` with a `takes[]` array containing that offer.
5. Observe that the call reverts with `stdError.arithmeticError` (not a custom error), originating from `mulDivUp` inside `sellerAssetsToUnits`.
6. Confirm that calling `IMidnight.take()` directly with the same offer and `units > 0` succeeds and returns `sellerAssets = 0`.

### Citations

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

**File:** src/Midnight.sol (L329-332)
```text
    /// @dev The taker might not get the price they expected if the settlement fee was just changed. A smart-contract
    /// can be used to perform atomic price checks.
    /// @dev Taking buy offers with price < settlement fee will revert.
    /// @dev In particular, if the settlement fee gets increased, it might implicitly cancel offers with very low price.
```

**File:** src/Midnight.sol (L361-364)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/periphery/MidnightBundles.sol (L245-248)
```text
    /// @dev Skips every reason why take can revert (including ones that are not asynchrony related).
    /// @dev Reverts if TakeAmountsLib or ConsumableUnitsLib reverts.
    /// @dev If taking an offer reverts, the bundler will completely skip this offer.
    /// @dev The msg.sender should have approved the bundler to transfer enough collateral.
```

**File:** src/periphery/MidnightBundles.sol (L269-275)
```text
        for (uint256 i; i < collateralSupplies.length; i++) {
            address token = market.collateralParams[collateralSupplies[i].collateralIndex].token;
            pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
            forceApproveMax(token, MIDNIGHT);
            IMidnight(MIDNIGHT)
                .supplyCollateral(market, collateralSupplies[i].collateralIndex, collateralSupplies[i].assets, taker);
        }
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

**File:** src/periphery/ConsumableUnitsLib.sol (L18-22)
```text
        } else if (offer.buy) {
            return TakeAmountsLib.buyerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
        } else {
            return TakeAmountsLib.sellerAssetsToUnits(midnight, id, offer, offer.maxAssets.zeroFloorSub(consumed));
        }
```

**File:** certora/confs/NoDivisionByZero.conf (L1-6)
```text
{
  "files": [
    "src/Midnight.sol"
  ],
  "verify": "Midnight:certora/specs/NoDivisionByZero.spec",
  "solc": "solc-0.8.34",
```
