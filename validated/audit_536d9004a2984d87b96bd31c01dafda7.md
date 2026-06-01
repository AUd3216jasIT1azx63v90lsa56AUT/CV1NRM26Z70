Audit Report

## Title
Seller Self-Activates Reverting-Oracle Collateral to DoS All Takers on Their Offers - (File: `src/Midnight.sol`)

## Summary
A seller with existing debt can call `supplyCollateral` on their own position to activate a collateral index whose oracle reverts. Because `isHealthy` iterates every bit in the seller's `collateralBitmap` and calls `IOracle.price()` with no `try/catch`, a single reverting oracle causes `isHealthy` to revert. The unconditional `require` at `Midnight.sol:476` propagates this revert through every subsequent `take()` against any of that seller's offers, freezing taker-side liquidity for as long as the seller carries debt.

## Finding Description

**`isHealthy` (`Midnight.sol:944-960`)** iterates the seller's `collateralBitmap` with a bare external call and no error handling:

```solidity
while (_collateralBitmap != 0) {
    uint256 i = UtilsLib.msb(_collateralBitmap);
    CollateralParams memory collateralParam = market.collateralParams[i];
    uint256 price = IOracle(collateralParam.oracle).price(); // no try/catch
    ...
    _collateralBitmap = _collateralBitmap.clearBit(i);
}
``` [1](#0-0) 

**`take()` (`Midnight.sol:475-476`)** unconditionally reaches `isHealthy` in the normal (non-reentrant) path:

```solidity
if (!wasLocked) UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, false);
require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
``` [2](#0-1) 

`wasLocked` is set at line 444 via `tExchange(..., true)` and returns the *previous* value. In a normal external call, the previous value is `false`, so `wasLocked = false`. Line 475 then clears the lock, making `liquidationLocked` return `false`, so `isHealthy` is always called. The `liquidationLocked` short-circuit only fires in a reentrant `take` inside a callback. [3](#0-2) 

**`supplyCollateral` (`Midnight.sol:523-546`)** sets a bit in `collateralBitmap` when `oldCollateral == 0 && assets > 0`. The authorization check (`onBehalf == msg.sender`) blocks third parties but explicitly permits the seller to call it on themselves:

```solidity
require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
...
if (oldCollateral == 0 && assets > 0) {
    uint128 newCollateralBitmap = _position.collateralBitmap.setBit(collateralIndex);
    _position.collateralBitmap = newCollateralBitmap;
    ...
}
``` [4](#0-3) 

There is no check that the oracle for the newly activated index is currently non-reverting.

**Exploit flow:**
1. Seller acquires debt via a prior `take` (becomes the seller in a sell offer or taker in a buy offer).
2. Seller calls `supplyCollateral(market, j, 1, seller)` where `collateralParams[j].oracle` is a reverting oracle. Cost: 1 wei of the collateral token.
3. Bit `j` is set in `position[id][seller].collateralBitmap`.
4. Any taker calls `take(offer, ...)` where the seller is the debt-holding party.
5. `isHealthy(market, id, seller)` is called at line 476.
6. The bitmap loop reaches index `j`, calls `IOracle(collateralParams[j].oracle).price()`, which reverts.
7. `isHealthy` reverts → `take` reverts with no useful error.
8. All takers are blocked from filling any of the seller's offers.

**Why existing checks fail:**
- The `liquidationLocked` short-circuit at line 476 only fires in the reentrant path (`wasLocked == true`). In the normal external call path it is always false after line 475 clears the lock.
- There is no `try/catch` around `IOracle.price()` in `isHealthy`.
- `supplyCollateral` has no oracle liveness check before activating a new collateral index.

**Formal confirmation:** The Certora spec `Reverts.spec` rule `oracleRevertPreventsTakeWhenSellerHasDebt` (lines 224–241) formally proves this revert path is reachable, and its inline comment explicitly notes the `liquidationLocked` short-circuit does not apply in the normal external call path: [5](#0-4) 

The `certora/README.md` also documents this as a known property: "A reverting or zero-returning collateral oracle blocks `liquidate`, `withdrawCollateral`, `isHealthy` and `take` whenever the borrower has debt." [6](#0-5) 

## Impact Explanation
Every `take()` call against any offer where the poisoned seller is the debt-holding party reverts at `Midnight.sol:476`. Takers cannot fill those offers at all. The seller's entire offer book is effectively frozen for takers. The seller retains their debt position and collateral; only taker-side liquidity is blocked. This constitutes a permanent, seller-controlled freeze of taker access to a specific offer book, achievable at a cost of 1 wei plus gas.

## Likelihood Explanation
**Required preconditions:** (a) the market lists at least two collateral params; (b) the seller has non-zero debt; (c) the seller can obtain 1 wei of the token for a collateral index whose oracle reverts or will revert. Condition (c) is feasible whenever a market lists a collateral whose oracle is upgradeable, pauseable, or otherwise fallible. Since market creation is permissionless, an attacker can also deploy a market with a reverting oracle as one of the collateral params and induce others to use it. The seller has direct economic incentive: if market rates move against them after signing offers, freezing takers prevents further debt accrual at unfavorable terms. The attack is repeatable and cheap.

## Recommendation
Two complementary fixes:

1. **Wrap oracle calls in `isHealthy` with `try/catch`:** Treat a reverting oracle as returning price `0`, which makes `maxDebt = 0` and causes `isHealthy` to return `false` (unhealthy) rather than reverting. This preserves the security invariant (a broken oracle makes the position liquidatable) while preventing DoS propagation.

2. **Add an oracle liveness check in `supplyCollateral`:** Before setting a new bit in `collateralBitmap`, call `IOracle(collateralParam.oracle).price()` (with `try/catch`) and revert if the oracle is currently non-functional. This prevents activation of broken oracles at the source.

Either fix alone breaks the attack chain; both together provide defense in depth.

## Proof of Concept
**Minimal Forge test outline:**
1. Deploy a market with two collateral params: `collateralParams[0]` with a working oracle, `collateralParams[1]` with a `RevertingOracle` that always reverts on `price()`.
2. Have the seller supply collateral at index 0 and acquire debt via a `take`.
3. Seller calls `supplyCollateral(market, 1, 1, seller)` — succeeds, sets bit 1 in bitmap.
4. A taker attempts `take(sellerOffer, ...)`.
5. Assert the call reverts (expected: revert propagated from `isHealthy` → `IOracle.price()` on index 1).
6. Seller calls `withdrawCollateral(market, 1, 1, seller, seller)` to clear bit 1.
7. Repeat step 4 — assert it now succeeds.

This directly demonstrates the freeze/unfreeze cycle controlled entirely by the seller.

### Citations

**File:** src/Midnight.sol (L444-444)
```text
        bool wasLocked = UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, true);
```

**File:** src/Midnight.sol (L475-476)
```text
        if (!wasLocked) UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, false);
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

**File:** src/Midnight.sol (L527-541)
```text
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        address collateralToken = market.collateralParams[collateralIndex].token;

        Position storage _position = position[id][onBehalf];
        uint256 oldCollateral = _position.collateral[collateralIndex];
        _position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets);

        if (oldCollateral == 0 && assets > 0) {
            uint128 newCollateralBitmap = _position.collateralBitmap.setBit(collateralIndex);
            _position.collateralBitmap = newCollateralBitmap;
            require(
                UtilsLib.countBits(newCollateralBitmap) <= MAX_COLLATERALS_PER_BORROWER, TooManyActivatedCollaterals()
            );
        }
```

**File:** src/Midnight.sol (L950-957)
```text
            while (_collateralBitmap != 0) {
                uint256 i = UtilsLib.msb(_collateralBitmap);
                CollateralParams memory collateralParam = market.collateralParams[i];
                uint256 price = IOracle(collateralParam.oracle).price();
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
                _collateralBitmap = _collateralBitmap.clearBit(i);
            }
```

**File:** certora/specs/Reverts.spec (L224-241)
```text
/// If an activated collateral oracle reverts on price and take succeeds, the seller must have no debt.
rule oracleRevertPreventsTakeWhenSellerHasDebt(env e, Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiver, address takerCallback, bytes takerCallbackData, uint256 collateralIndex) {
    require singleRevertingOracle == offer.market.collateralParams[collateralIndex].oracle, "oracle is reverting";

    bytes32 id = summaryToId(offer.market);
    address seller = offer.buy ? taker : offer.maker;

    // Without this, take's liquidatability check short-circuits to false (without calling isHealthy) because
    // take's tExchange keeps the lock set when wasLocked is true, so the oracle is never queried.
    require !liquidationLocked(id, seller), "seller is not liquidation locked";

    uint128 bitmap = collateralBitmap(id, seller);
    require summaryGetBit(bitmap, collateralIndex), "collateralIndex is activated";

    take(e, offer, ratifierData, units, taker, receiver, takerCallback, takerCallbackData);

    assert debtOf(id, seller) == 0;
}
```

**File:** certora/README.md (L69-72)
```markdown
- [`Reverts.spec`](specs/Reverts.spec) checks some failures reasons.
  A reverting or zero-returning collateral oracle blocks `liquidate`, `withdrawCollateral`, `isHealthy` and `take` whenever the borrower has debt.
  The liquidator (resp. enter) gate blocks liquidation (resp. credit increase and debt increase).
  A reverting `transfer`/`transferFrom` or callback (including a wrong return value) makes the calling entry point revert.
```
