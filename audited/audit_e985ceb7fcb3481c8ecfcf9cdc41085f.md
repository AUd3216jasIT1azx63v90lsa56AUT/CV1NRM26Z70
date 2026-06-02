Audit Report

## Title
`sellerAssetsToUnits` reverts on division-by-zero when `offerPrice == settlementFee`, DoS-ing `supplyCollateralAndSellWithAssetsTarget` - (File: src/periphery/TakeAmountsLib.sol)

## Summary
`TakeAmountsLib.sellerAssetsToUnits` computes `sellerPrice = offerPrice - settlementFee` for buy offers and passes it as the divisor to `mulDivUp`. When `offerPrice == settlementFee`, `sellerPrice = 0`, causing `mulDivUp` to revert via arithmetic underflow at `(d - 1)` with `d = 0`. The core `Midnight.take()` handles this case without reverting (seller receives zero assets). Because `supplyCollateralAndSellWithAssetsTarget` calls `sellerAssetsToUnits` outside its `try/catch` block, the revert propagates to the caller, permanently DoS-ing that entry-point for any bundle whose `takes[]` array includes such an offer.

## Finding Description

**Root cause — `TakeAmountsLib.sellerAssetsToUnits` (`src/periphery/TakeAmountsLib.sol` lines 44–46):**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
return
    offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...;
```

When `offer.buy == true` and `offerPrice == settlementFee`, `sellerPrice = 0`. The `mulDivUp` implementation in `UtilsLib.sol` is:

```solidity
function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
    return (x * y + (d - 1)) / d;
}
```

With `d = 0`, the expression `(d - 1)` underflows in Solidity 0.8+, causing an unconditional revert before the division even executes. [1](#0-0) [2](#0-1) 

**Core `take()` does NOT revert in the same scenario (`src/Midnight.sol` lines 361–364):**

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice; // = 0
uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : ...; // = (units * 0) / WAD = 0
```

`mulDivDown(units, 0, WAD)` multiplies first (result 0) then divides by `WAD` — no underflow, no revert. `take()` succeeds and the seller receives zero assets. [3](#0-2) [4](#0-3) 

**`supplyCollateralAndSellWithAssetsTarget` calls `sellerAssetsToUnits` outside the `try/catch` (`src/periphery/MidnightBundles.sol` lines 285–300):**

```solidity
uint256 unitsToTake = min(
    TakeAmountsLib.sellerAssetsToUnits(   // ← NOT inside try/catch
        MIDNIGHT, id, takes[i].offer, targetFilledSellerAssets - filledSellerAssets
    ),
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
);
try IMidnight(MIDNIGHT).take(...) ...     // ← only take() is guarded
```

The NatSpec on the function explicitly acknowledges this: *"Reverts if TakeAmountsLib or ConsumableUnitsLib reverts."* A revert from `sellerAssetsToUnits` is not caught and bubbles up, reverting the entire bundle call. [5](#0-4) 

**Existing protections are insufficient:** The NatSpec comment on `sellerAssetsToUnits` documents revert only for `offerPrice < settlementFee`. The `==` case is undocumented and unguarded. [6](#0-5) 

## Impact Explanation
Any call to `supplyCollateralAndSellWithAssetsTarget` whose `takes[]` array contains a buy offer at the settlement-fee price point reverts entirely. The attacker can grief any taker who includes that offer, permanently blocking the periphery sell path for that bundle. Because the offer itself is valid and `take()` would accept it, the DoS is invisible to the taker until the bundle reverts. This constitutes a griefing/DoS of a core periphery entry-point with no capital cost to the attacker.

## Likelihood Explanation
Settlement fees are discrete values set by governance; tick prices are also discrete. The condition `tickToPrice(T) == settlementFee` is achievable whenever the fee is set to a value that coincides with any accessible tick price. An unprivileged attacker can monitor the fee, identify the matching tick, and post a buy offer there at negligible cost (a buy offer with `maxUnits = 0` or a small amount requires no capital at risk). The attack is repeatable: a new offer can be posted after each cancellation.

## Recommendation
Add a guard in `sellerAssetsToUnits` for the `sellerPrice == 0` case. When `sellerPrice == 0`, the seller receives zero assets regardless of units taken, so no finite number of units can satisfy a nonzero `targetSellerAssets`. Return `type(uint256).max` to signal this (the `min()` in the caller will cap it to `takes[i].units` or `consumableUnits`, and the loop will eventually exhaust all offers and revert with `OutOfOffers` — the correct behavior). Alternatively, add an explicit `require(sellerPrice > 0)` to match the documented invariant.

```solidity
uint256 sellerPrice = offer.buy ? offerPrice - settlementFee : offerPrice;
if (sellerPrice == 0) return type(uint256).max; // seller receives 0 assets; no units satisfy a nonzero target
return offer.buy ? targetSellerAssets.mulDivUp(WAD, sellerPrice) : ...;
```

## Proof of Concept
1. Deploy Midnight with a market whose settlement fee is set to a value `F` that equals `tickToPrice(T)` for some accessible tick `T`.
2. Attacker (unprivileged maker) posts a buy offer at tick `T` with `maxUnits > 0`.
3. Victim calls `supplyCollateralAndSellWithAssetsTarget` with `takes[0]` pointing to the attacker's offer.
4. The call reaches `TakeAmountsLib.sellerAssetsToUnits(...)` with `sellerPrice = 0`.
5. `mulDivUp(targetSellerAssets, WAD, 0)` executes `(targetSellerAssets * WAD + (0 - 1))` — underflow revert in Solidity 0.8+.
6. The entire bundle call reverts; the victim's collateral supply is rolled back.
7. Calling `IMidnight.take()` directly on the same offer succeeds and returns `sellerAssets = 0`.

### Citations

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

**File:** src/Midnight.sol (L361-364)
```text
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/periphery/MidnightBundles.sol (L244-300)
```text
    /// @dev The collateral transfers always use the first offer's market.
    /// @dev Skips every reason why take can revert (including ones that are not asynchrony related).
    /// @dev Reverts if TakeAmountsLib or ConsumableUnitsLib reverts.
    /// @dev If taking an offer reverts, the bundler will completely skip this offer.
    /// @dev The msg.sender should have approved the bundler to transfer enough collateral.
    /// @dev Total loan assets received by the receiver is targetSellerAssets.
    /// @dev The taker will lose at most maxUnits.
    /// @dev The referral fee changes the amount that must be filled, which can change the average taking price.
    function supplyCollateralAndSellWithAssetsTarget(
        uint256 targetSellerAssets,
        uint256 maxUnits,
        address taker,
        address receiver,
        CollateralSupply[] memory collateralSupplies,
        Take[] memory takes,
        uint256 referralFeePct,
        address referralFeeRecipient
    ) external {
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
        require(referralFeePct < WAD, PctExceeded());
        address loanToken = takes[0].offer.market.loanToken;
        // touchMarket to have the correct settlement fees.
        bytes32 id = IMidnight(MIDNIGHT).touchMarket(takes[0].offer.market);

        Market memory market = takes[0].offer.market;
        for (uint256 i; i < collateralSupplies.length; i++) {
            address token = market.collateralParams[collateralSupplies[i].collateralIndex].token;
            pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
            forceApproveMax(token, MIDNIGHT);
            IMidnight(MIDNIGHT)
                .supplyCollateral(market, collateralSupplies[i].collateralIndex, collateralSupplies[i].assets, taker);
        }

        uint256 referralFeeAssets = targetSellerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
        uint256 targetFilledSellerAssets = targetSellerAssets + referralFeeAssets;

        uint256 filledUnits;
        uint256 filledSellerAssets;
        for (uint256 i; i < takes.length && filledSellerAssets < targetFilledSellerAssets; i++) {
            require(takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
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
