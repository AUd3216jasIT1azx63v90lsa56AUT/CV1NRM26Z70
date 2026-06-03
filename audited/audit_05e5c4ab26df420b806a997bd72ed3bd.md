I have read all the relevant source files. Let me now validate each claim against the actual code.

**Claim 1: No `if (unitsToTake == 0) continue;` guard before the `try` call.**

Confirmed. All four bundle loops compute `unitsToTake` and immediately dispatch `try IMidnight(MIDNIGHT).take(...)` with no zero-check. [1](#0-0) 

**Claim 2: `ConsumableUnitsLib.consumableUnits()` returns 0 when `consumed >= maxUnits`.**

Confirmed. `zeroFloorSub` is `mul(gt(x, y), sub(x, y))`, which evaluates to 0 when `x <= y`. [2](#0-1) [3](#0-2) 

**Claim 3: `setConsumed` is callable by any unprivileged maker on their own behalf.**

Confirmed. The only authorization check is `onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender]`, so any user can call it for themselves. [4](#0-3) 

**Claim 4: `take()` with `units=0` and `consumed == maxUnits` passes the consumed check.**

Confirmed. `newConsumed = consumed[maker][group] += 0 = maxUnits`, and `require(maxUnits <= maxUnits)` passes. [5](#0-4) 

**Claim 5: The ratifier external call executes before the consumed check.**

Confirmed. Line 356 (`IRatifier.isRatified`) precedes the consumed check at lines 367–373. [6](#0-5) 

**Claim 6: Protocol comment at line 93 explicitly permits no-op takes on fully-consumed offers.**

Confirmed. [7](#0-6) 

**Claim 7: `_updatePosition` calls are conditional on credit.**

Confirmed, and the report accurately states "if they hold credit." [8](#0-7) 

**Claim 8: Two `safeTransferFrom` calls with amount 0 execute.**

Confirmed. With `units=0`, `buyerAssets = 0.mulDivDown(...) = 0` and `sellerAssets = 0`. [9](#0-8) 

**Claim 9: `isHealthy` is called unconditionally at the end of `take()`.**

Confirmed. If the seller has no debt, `isHealthy` returns true without calling any oracle (line 948 short-circuits), so the oracle cost is conditional on seller debt. [10](#0-9) 

**Claim 10: Loop does not terminate early; transaction reverts with `OutOfOffers()`.**

Confirmed. `filledUnits += 0` leaves the loop condition `filledUnits < targetUnits` true; after exhausting `takes.length`, the `require` reverts. [11](#0-10) 

**Claim 11: Same missing guard in all four bundle functions.**

Confirmed at lines 74–85, 147–160, 208–221, and 285–300. [12](#0-11) [13](#0-12) [14](#0-13) 

**One minor inaccuracy in the report:** The report states the `try/catch` "silently swallows both reverts and zero-return successes." In reality, zero-return successes execute the *success* branch (not the catch), adding 0 to both counters. The net effect is identical — no progress — but the mechanism differs slightly from the description. This does not affect the validity of the finding.

**Rejection-rule check:** The rules say to reject "gas-only" issues. This is not a pure gas-optimization issue; it is a targeted, repeatable DoS attack where the victim loses real ETH (gas) and cannot complete their intended trade. The attacker requires no privilege beyond controlling a maker address, which any user qualifies for. The attack is executable without front-running via social engineering (listing exhausted offers in an off-chain order book). This meets the bar for a valid in-scope finding.

---

Audit Report

## Title
Unbounded zero-unit take loop in bundle functions allows gas griefing via fully-consumed group front-run - (File: src/periphery/MidnightBundles.sol)

## Summary
All four bundle functions in `MidnightBundles.sol` dispatch `IMidnight.take()` with `units=0` when `ConsumableUnitsLib.consumableUnits()` returns 0 for a fully-consumed group, because no guard skips the `try` call when `unitsToTake == 0`. Since `Midnight.take()` explicitly permits no-op takes on fully-consumed offers (line 93 comment), each such call succeeds and returns `(0, 0)`, leaving `filledUnits` unchanged. An attacker who controls the maker address can front-run a taker's bundle transaction by calling `setConsumed(group, type(uint256).max, maker)`, converting every offer in the `takes[]` array into a zero-unit no-op and forcing the taker to burn O(N × per-take gas) before the transaction reverts with `OutOfOffers()`.

## Finding Description

**Root cause:** No `if (unitsToTake == 0) continue;` guard exists before the `try` call in any of the four bundle loops.

**Exact code path in `buyWithUnitsTargetAndWithdrawCollateral` (lines 74–85):**

`unitsToTake` is computed as the three-way minimum:

```solidity
uint256 unitsToTake = min(
    targetUnits - filledUnits,
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
);
```

`ConsumableUnitsLib.consumableUnits()` returns `offer.maxUnits.zeroFloorSub(consumed)`, which is 0 when `consumed >= maxUnits`. `zeroFloorSub` is implemented as `mul(gt(x, y), sub(x, y))`, returning 0 when `x <= y`. So `unitsToTake = min(..., 0) = 0`. The `try` is dispatched unconditionally with `units=0`.

Inside `Midnight.take()`, with `units=0` and `consumed == maxUnits`, the consumed check evaluates `newConsumed = maxUnits + 0 = maxUnits`, so `require(maxUnits <= maxUnits)` passes. Before the consumed check, the ratifier external call at line 356 executes unconditionally. With `units=0`, `buyerAssets=0` and `sellerAssets=0`; all position arithmetic is a no-op; a `Take` event is emitted; a TSTORE liquidation lock is set; two `safeTransferFrom` calls with amount 0 execute; and an `isHealthy` call runs (invoking an oracle if the seller has debt). `take()` returns `(0, 0)`. Back in the bundle, `filledUnits += 0`, the loop condition `filledUnits < targetUnits` remains true, and the next iteration begins. After all N iterations the loop exits by exhausting `takes.length`, and `require(filledUnits == targetUnits, OutOfOffers())` reverts the entire transaction.

`setConsumed` is callable by the maker (any unprivileged user acting as their own `onBehalf`):

```solidity
function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    require(amount >= consumed[onBehalf][group], AlreadyConsumed());
    consumed[onBehalf][group] = amount;
}
```

**Why existing checks fail:**
- The `try/catch` at lines 79–85 is designed to tolerate asynchrony. When `take()` succeeds with `(0, 0)`, the success branch executes and adds 0 to both counters — providing no protection against this case.
- The loop condition `filledUnits < targetUnits` does not terminate early because `filledUnits` never advances when all takes return 0.
- `ConsumableUnitsLib.consumableUnits()` correctly returns 0, but the bundle does not act on this signal before dispatching the take.

The same missing guard exists identically in all four bundle functions:
- `buyWithUnitsTargetAndWithdrawCollateral` (lines 74–85)
- `supplyCollateralAndSellWithUnitsTarget` (lines 147–160)
- `buyWithAssetsTargetAndWithdrawCollateral` (lines 208–221)
- `supplyCollateralAndSellWithAssetsTarget` (lines 285–300)

## Impact Explanation
Every zero-unit take in `Midnight.take()` executes: one external ratifier call (line 356), conditional `_updatePosition` calls with multiple SLOADs/SSTOREs (lines 379–380), a `claimableSettlementFee` SLOAD+SSTORE (line 418), a `Take` event emission (lines 425–442), a TSTORE liquidation lock (line 444), two `safeTransferFrom` calls with amount 0 (lines 455–456), and an `isHealthy` call that may invoke an oracle (line 476). Each zero-unit take costs on the order of 30,000–80,000 gas depending on position state and oracle complexity. For N = 100 offers, the taker's transaction burns 3,000,000–8,000,000 gas before reverting with `OutOfOffers()`. The taker loses all gas; no funds are transferred because the transaction reverts. The attack is a repeatable, targeted DoS against any user of the bundle functions.

## Likelihood Explanation
**Preconditions:**
1. Attacker controls a maker address — any unprivileged user qualifies.
2. Attacker creates N offers sharing the same `(maker, group)` pair — no cost beyond gas.
3. Attacker calls `setConsumed(group, type(uint256).max, maker)` — one cheap transaction, callable by the maker on their own behalf.
4. A victim taker calls a bundle function with those N offers in `takes[]`.

The front-run variant is straightforward on any chain with a public mempool. The social-engineering variant (listing N exhausted offers in an off-chain order book) requires no front-running at all. The attack is repeatable: the attacker can re-exhaust a group after any partial fill, or create new groups. Likelihood is medium-high given that bundle functions are designed for use with aggregators and order-book integrations that fetch offer lists off-chain.

## Recommendation
Add an early-continue guard in each bundle loop immediately after computing `unitsToTake`:

```solidity
uint256 unitsToTake = min(
    targetUnits - filledUnits,
    takes[i].units,
    ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
);
if (unitsToTake == 0) continue;  // skip fully-consumed or zero-sized offers
try IMidnight(MIDNIGHT).take(..., unitsToTake, ...) ...
```

Apply the same fix to all four bundle functions. This eliminates the no-op take dispatch entirely when the offer is fully consumed, at negligible cost.

## Proof of Concept
**Minimal manual steps:**
1. Deploy `Midnight` and `MidnightBundles` on a local fork.
2. As attacker (maker), create N sell offers in the same group with `maxUnits = 1000`.
3. Call `setConsumed(group, type(uint256).max, attacker)` to fully consume the group.
4. As victim, call `buyWithUnitsTargetAndWithdrawCollateral` with `targetUnits = 1` and `takes[]` containing all N attacker offers.
5. Observe: the transaction executes N calls to `Midnight.take()` with `units=0`, each succeeding and returning `(0, 0)`, then reverts with `OutOfOffers()`. Measure gas consumed vs. a baseline with a single valid offer.

**Fuzz/invariant test plan:** Fuzz `takes[]` arrays containing a mix of valid and fully-consumed offers; assert that the gas consumed by a reverting bundle call is bounded by `O(takes.length)` times a per-take constant, and that no call to `take()` with `unitsToTake == 0` is dispatched.

### Citations

**File:** src/periphery/MidnightBundles.sol (L71-88)
```text
        for (uint256 i; i < takes.length && filledUnits < targetUnits; i++) {
            require(!takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
            uint256 unitsToTake = min(
                targetUnits - filledUnits,
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
            try IMidnight(MIDNIGHT)
                .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (
                uint256 resBuyerAssets, uint256
            ) {
                filledUnits += unitsToTake;
                filledBuyerAssets += resBuyerAssets;
            } catch {}
        }

        require(filledUnits == targetUnits, OutOfOffers());
```

**File:** src/periphery/MidnightBundles.sol (L147-160)
```text
            uint256 unitsToTake = min(
                targetUnits - filledUnits,
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

**File:** src/periphery/MidnightBundles.sol (L208-221)
```text
            uint256 unitsToTake = min(
                TakeAmountsLib.buyerAssetsToUnits(
                    MIDNIGHT, id, takes[i].offer, targetFilledBuyerAssets - filledBuyerAssets
                ),
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
            try IMidnight(MIDNIGHT)
                .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (
                uint256 resBuyerAssets, uint256
            ) {
                filledUnits += unitsToTake;
                filledBuyerAssets += resBuyerAssets;
            } catch {}
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

**File:** src/periphery/ConsumableUnitsLib.sol (L16-17)
```text
        if (offer.maxUnits > 0) {
            return offer.maxUnits.zeroFloorSub(consumed);
```

**File:** src/libraries/UtilsLib.sol (L22-26)
```text
    function zeroFloorSub(uint256 x, uint256 y) internal pure returns (uint256 z) {
        assembly {
            z := mul(gt(x, y), sub(x, y))
        }
    }
```

**File:** src/Midnight.sol (L93-93)
```text
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
```

**File:** src/Midnight.sol (L355-356)
```text
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

**File:** src/Midnight.sol (L370-373)
```text
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
```

**File:** src/Midnight.sol (L379-380)
```text
        if (hasCredit(id, buyer) || units > buyerPos.debt) _updatePosition(offer.market, id, buyer);
        if (hasCredit(id, seller)) _updatePosition(offer.market, id, seller);
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/Midnight.sol (L476-476)
```text
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

**File:** src/Midnight.sol (L723-728)
```text
    function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        require(amount >= consumed[onBehalf][group], AlreadyConsumed());
        consumed[onBehalf][group] = amount;
        emit EventsLib.SetConsumed(msg.sender, group, amount, onBehalf);
    }
```
