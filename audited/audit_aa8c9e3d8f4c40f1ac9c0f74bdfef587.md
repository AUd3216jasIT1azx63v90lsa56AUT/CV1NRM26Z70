I have read all the relevant source files. Here is my validation analysis followed by the output.

**Validation of each claim:**

**Missing guard (root cause):** Confirmed. All four bundle loops compute `unitsToTake` and pass it directly to `try IMidnight(MIDNIGHT).take(...)` with no `if (unitsToTake == 0) continue;` guard. [1](#0-0) 

**`consumableUnits()` returns 0 for exhausted offers:** Confirmed. `offer.maxUnits.zeroFloorSub(consumed)` where `zeroFloorSub` is `z := mul(gt(x, y), sub(x, y))` — returns 0 when `consumed >= maxUnits`. [2](#0-1) [3](#0-2) 

**`setConsumed` callable by any maker:** Confirmed. `require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender])` — any user can call it for themselves. [4](#0-3) 

**Mechanism correction — two valid sub-vectors:**

*Sub-vector A (`consumed = maxUnits` exactly):* `take(units=0)` succeeds. `newConsumed = maxUnits + 0 = maxUnits`, `require(maxUnits <= maxUnits)` passes. The full take path executes — ratifier call (line 356), conditional `_updatePosition` (lines 379–380), position arithmetic, `Take` event, TSTORE, two zero-amount `safeTransferFrom` calls, `isHealthy`. Returns `(0, 0)`. `filledUnits += 0`. Loop continues. [5](#0-4) [6](#0-5) 

*Sub-vector B (`consumed = type(uint256).max`):* The report's stated attack vector. `take(units=0)` **reverts** — `newConsumed = type(uint256).max`, `require(type(uint256).max <= offer.maxUnits)` fails. The `try/catch` catches the revert. However, the ratifier external call at line 356 executes **before** the consumed check, so gas is still burned. The report's claim that `take()` "succeeds and returns `(0, 0)`" is inaccurate for this vector; the gas cost per iteration is lower than claimed (no `_updatePosition`, no event, no TSTORE, no transfers, no `isHealthy`). The attacker should use sub-vector A (`setConsumed(group, maxUnits, maker)`) to achieve the full gas cost described. [7](#0-6) 

**Protocol comment confirming intentional no-op take behavior:** Confirmed at line 93. [8](#0-7) 

**Loop never terminates early:** Confirmed. `filledUnits += unitsToTake` where `unitsToTake = 0` leaves `filledUnits` unchanged; `require(filledUnits == targetUnits, OutOfOffers())` reverts the entire transaction. [9](#0-8) 

**SECURITY.md scope:** Gas-griefing-only issues are not explicitly excluded for smart contracts. The impact here is a targeted, repeatable DoS (victim's transaction is forced to revert, preventing trade completion) with gas loss — not merely a gas-optimization issue. [10](#0-9) 

All four bundle functions are identically affected. [11](#0-10) [12](#0-11) [13](#0-12) 

---

Audit Report

## Title
Unbounded zero-unit take loop in bundle functions allows gas griefing via fully-consumed group front-run - (File: src/periphery/MidnightBundles.sol)

## Summary
All four bundle functions in `MidnightBundles.sol` dispatch `IMidnight.take()` with `units=0` when `ConsumableUnitsLib.consumableUnits()` returns 0 for a fully-consumed group, because no guard skips the `try` call when `unitsToTake == 0`. An attacker who controls a maker address can exhaust their own group (via `setConsumed`) and list those offers in an off-chain order book, or front-run a victim's bundle transaction, forcing the victim to burn gas on N no-op or reverted takes before the transaction reverts with `OutOfOffers()`. The victim loses all gas; no funds are transferred because the transaction reverts.

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

`ConsumableUnitsLib.consumableUnits()` returns `offer.maxUnits.zeroFloorSub(consumed)`, which is 0 when `consumed >= maxUnits`. `zeroFloorSub` is implemented as `z := mul(gt(x, y), sub(x, y))`, returning 0 when `x <= y`. So `unitsToTake = min(..., 0) = 0`.

The `try` is dispatched unconditionally with `units=0`.

**Two valid attack sub-vectors:**

*Sub-vector A — full no-op take (attacker calls `setConsumed(group, maxUnits, maker)`):**
Inside `Midnight.take()` with `units=0` and `consumed == maxUnits`:
- `newConsumed = maxUnits + 0 = maxUnits`; `require(maxUnits <= maxUnits)` passes (line 372).
- The ratifier external call executes (line 356).
- Conditional `_updatePosition` calls execute for buyer/seller if they hold credit (lines 379–380).
- Position arithmetic, `Take` event emission, TSTORE liquidation lock, two `safeTransferFrom` calls with amount 0, and `isHealthy` (potentially invoking an oracle) all execute.
- `take()` returns `(0, 0)`. `filledUnits += 0`. Loop continues.

*Sub-vector B — reverted take (attacker calls `setConsumed(group, type(uint256).max, maker)`):**
- `consumableUnits()` returns 0 (same path).
- Inside `take()`, `newConsumed = type(uint256).max`; `require(type(uint256).max <= offer.maxUnits)` reverts with `ConsumedUnits()`.
- The ratifier external call at line 356 executes **before** the consumed check and is not rolled back from the gas perspective.
- The `catch {}` block swallows the revert. `filledUnits` does not advance. Loop continues.
- Gas cost per iteration is lower than sub-vector A (no `_updatePosition`, event, TSTORE, transfers, or `isHealthy`), but still includes the ratifier call.

**Why existing checks fail:**
- The `try/catch` is designed to tolerate asynchrony (offer taken by someone else between block construction and execution). It silently swallows both reverts and zero-return successes, providing no protection.
- The loop condition `filledUnits < targetUnits` does not terminate early because `filledUnits` never advances.
- `ConsumableUnitsLib.consumableUnits()` correctly returns 0, but the bundle does not act on this signal before dispatching the take.

The same missing guard exists identically in all four bundle functions:
- `buyWithUnitsTargetAndWithdrawCollateral` (lines 74–85)
- `supplyCollateralAndSellWithUnitsTarget` (lines 147–160)
- `buyWithAssetsTargetAndWithdrawCollateral` (lines 208–221)
- `supplyCollateralAndSellWithAssetsTarget` (lines 285–300)

After all N iterations the loop exits by exhausting `takes.length`, and `require(filledUnits == targetUnits, OutOfOffers())` reverts the entire transaction.

## Impact Explanation
Using sub-vector A, every zero-unit take in `Midnight.take()` executes: one external ratifier call (line 356), up to two `_updatePosition` calls with multiple SLOADs/SSTOREs (lines 379–380), position arithmetic touching storage slots (lines 408–414), a `claimableSettlementFee` SLOAD+SSTORE (line 418), a `Take` event emission (lines 425–442), a TSTORE liquidation lock (line 444), two `safeTransferFrom` calls with amount 0 (lines 455–456), and an `isHealthy` call that may invoke an oracle (line 476). Each zero-unit take costs on the order of 30,000–80,000 gas depending on position state and oracle complexity. For N = 100 offers, the taker's transaction burns 3,000,000–8,000,000 gas before reverting with `OutOfOffers()`. The taker loses all gas; no funds are transferred because the transaction reverts. The attack is a repeatable, targeted DoS against any user of the bundle functions.

## Likelihood Explanation
**Preconditions:**
1. Attacker controls a maker address — any unprivileged user qualifies.
2. Attacker creates N offers sharing the same `(maker, group)` pair — no cost beyond gas.
3. Attacker calls `setConsumed(group, maxUnits, maker)` — one cheap transaction, callable by the maker on their own behalf.
4. A victim taker calls a bundle function with those N offers in `takes[]`.

The front-run variant is straightforward on any chain with a public mempool. The social-engineering variant (listing N exhausted offers in an off-chain order book) requires no front-running at all. The attack is repeatable: the attacker can create new groups at will. Likelihood is medium-high given that bundle functions are designed for use with aggregators and order-book integrations that fetch offer lists off-chain.

## Recommendation
Add an early-continue guard before each `try` call in all four bundle loops:

```solidity
if (unitsToTake == 0) continue;
```

This single-line fix, applied identically in all four functions, eliminates the ability to dispatch a take with zero units and prevents the gas-griefing loop.

## Proof of Concept
**Minimal manual steps:**
1. Deploy `Midnight` and `MidnightBundles` on a local fork.
2. Attacker creates N sell offers (e.g., N = 50) with the same `(maker, group)` pair and `maxUnits = 1000`.
3. Attacker calls `setConsumed(group, 1000, maker)` — sets consumed exactly to `maxUnits`.
4. Victim calls `buyWithUnitsTargetAndWithdrawCollateral` with `targetUnits = 1` and `takes[]` containing all N attacker offers.
5. Observe: `consumableUnits()` returns 0 for each offer; `take()` is called 50 times with `units=0`; each call succeeds and returns `(0, 0)`; `filledUnits` remains 0; transaction reverts with `OutOfOffers()` after burning ~50× per-take gas.
6. Measure gas consumed vs. a baseline call with a single valid offer — confirm O(N) gas waste.

**Fuzz test plan:** Fuzz `takes.length` (1–200) with all offers having `consumed == maxUnits`; assert that gas consumed grows linearly with `takes.length` and that the transaction always reverts with `OutOfOffers()`.

### Citations

**File:** src/periphery/MidnightBundles.sol (L74-88)
```text
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

**File:** src/periphery/MidnightBundles.sol (L144-161)
```text
        for (uint256 i; i < takes.length && filledUnits < targetUnits; i++) {
            require(takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
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
        }
```

**File:** src/periphery/MidnightBundles.sol (L205-222)
```text
        for (uint256 i; i < takes.length && filledBuyerAssets < targetFilledBuyerAssets; i++) {
            require(!takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
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
        }
```

**File:** src/periphery/MidnightBundles.sol (L282-301)
```text
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
        }
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

**File:** src/Midnight.sol (L354-356)
```text
        require(offer.maker != taker, SelfTake());
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

**File:** src/Midnight.sol (L366-373)
```text
        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
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

**File:** SECURITY.md (L18-26)
```markdown
### Smart Contracts / Blockchain DLT

- Incorrect data supplied by third-party oracles.
- Impacts requiring basic economic and governance attacks (e.g. 51% attack).
- Lack of liquidity impacts.
- Impacts from Sybil attacks.
- Impacts involving centralization risks.

Note: This does not exclude oracle manipulation/flash-loan attacks.
```
