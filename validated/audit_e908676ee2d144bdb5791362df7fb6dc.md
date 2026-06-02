Audit Report

## Title
Zero-asset rounding bypass allows unbounded position mutation on fully-consumed assets-based buy offers — (File: src/Midnight.sol)

## Summary
When a buy offer uses `maxAssets`-based consumption and `offerPrice < WAD`, calling `take()` with `units=1` produces `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`. The consumed cap check advances by zero and passes unconditionally, while position credit, debt, and `totalUnits` mutate as if a real trade occurred — with zero tokens transferred. This is repeatable without bound on any exhausted qualifying offer.

## Finding Description

**Root cause** — `src/Midnight.sol`, `take()`:

```solidity
// Line 363
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : ...;
// units=1, buyerPrice<WAD → mulDivDown(1, buyerPrice, WAD) = 0

// Lines 367–369
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += buyerAssets; // += 0
    require(newConsumed <= offer.maxAssets, ConsumedAssets());       // trivially passes
}
``` [1](#0-0) 

The `ConsumedAssets` check is the sole guard preventing execution on an exhausted offer. Because `buyerAssets` rounds to zero, `newConsumed` does not advance, and the require is satisfied regardless of how many times the call is repeated.

**Execution continues unconditionally after the guard:**

```solidity
// Lines 382–384
uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt); // = 1 if no debt
uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);       // = 0 if no credit
uint256 sellerDebtIncrease = units - sellerCreditDecrease;                  // = 1

// Lines 408–414
buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);  // +1
sellerPos.debt  += UtilsLib.toUint128(sellerDebtIncrease);   // +1

// Lines 416–417
_marketState.totalUnits += buyerCreditIncrease - sellerCreditDecrease; // +1

// Lines 445–453: onBuy callback fires with buyerAssets=0, units=1
// Lines 455–456: safeTransferFrom(..., 0) — no revert
``` [2](#0-1) [3](#0-2) [4](#0-3) 

**Protocol acknowledgment of the behavior:**

Line 94 states: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* This confirms developer awareness of the rounding path. [5](#0-4) 

The Certora README claims: *"once at the max it stops moving: a fully-consumed offer then admits only no-op takes."* The test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` directly falsifies this claim — it asserts post-take that `creditOf(maker) > before`, `debtOf(taker) > before`, and `totalUnits > before`, while `consumed` and token balances are unchanged. [6](#0-5) 

**Existing checks reviewed and shown insufficient:**

- `ConsumedAssets` check (line 369): bypassed because `buyerAssets = 0`.
- `CannotIncreaseDebtPostMaturity` (line 391): only blocks post-maturity; pre-maturity debt increase is unrestricted.
- `SellerIsLiquidatable` health check (line 476): limits total debt accumulation to the seller's collateral value, but does not prevent the attack — it only bounds the number of iterations before the seller becomes unhealthy.
- `SelfTake` check (line 354): prevents the maker from calling `take()` on their own offer directly, but does not prevent a third party (e.g., automated router) from doing so. [7](#0-6) [8](#0-7) [9](#0-8) 

## Impact Explanation

1. **Free credit for the maker (buyer)**: Each call increments `buyerPos.credit` by 1 at zero token cost. Credit is a claim on loan tokens at settlement; inflating it allows the maker to redeem more assets than were ever deposited, constituting direct theft from the protocol's asset pool at the expense of other lenders.
2. **Debt imposed on the taker (seller) without asset receipt**: `sellerPos.debt` increases by 1 per call. A victim taker (e.g., an automated router that does not verify consumed state before calling `take()`) accumulates debt with no corresponding loan token receipt, and must repay at settlement.
3. **`totalUnits` diverges from real asset flows**: Protocol-wide accounting invariants break. The Certora-verified invariant that fully-consumed offers admit only no-op takes is violated, undermining the formal verification guarantees.
4. **Uncontrolled callback invocation**: The maker's `onBuy` callback fires with `buyerAssets=0, units=1` on every call, which can corrupt callback-side state that assumes it is only triggered by real asset flows.

## Likelihood Explanation

**Required preconditions:**
- `offer.buy = true`, `offer.maxAssets > 0` — standard buy offer configuration, no special access.
- `offerPrice < WAD` — any tick below the WAD-price tick; the confirmed test uses `MAX_TICK - 16`.
- `consumed[maker][group] == maxAssets` — reachable after a normal full fill, or self-set by the maker via `setConsumed` (permissionless for the maker on their own group).

All preconditions are reachable by an unprivileged external user. The attack requires zero capital (zero tokens transferred), is repeatable without bound on every qualifying exhausted offer (up to the seller's health limit), and leaves no on-chain trace distinguishing it from a legitimate take. The maker can construct the offer and wait for any automated taker to interact.

## Recommendation

Add a minimum-assets guard before position mutation. Specifically, revert when `buyerAssets == 0` on a buy offer (and `sellerAssets == 0` on a sell offer) to prevent zero-value takes from mutating state:

```solidity
// After computing buyerAssets/sellerAssets, before the consumed check:
require(offer.buy ? buyerAssets > 0 : sellerAssets > 0, ZeroAssets());
```

Alternatively, enforce a minimum `units` input such that `mulDivDown(units, buyerPrice, WAD) >= 1` for any valid price, or use `mulDivUp` for the consumed accounting increment to ensure at least 1 asset is counted even when rounding down produces zero.

## Proof of Concept

The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` constitutes a reproducible PoC. It:
1. Creates a buy offer with `maxAssets > 0` and `offerPrice < WAD` (tick = `MAX_TICK - 16`).
2. Fully consumes the offer via a normal `take()`.
3. Calls `take()` again with `units=1` on the exhausted offer — no `vm.expectRevert`.
4. Asserts `creditOf(maker) > before`, `debtOf(taker) > before`, `totalUnits > before`, while `consumed` and token balances are unchanged. [5](#0-4)

### Citations

**File:** src/Midnight.sol (L93-94)
```text
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L354-354)
```text
        require(offer.maker != taker, SelfTake());
```

**File:** src/Midnight.sol (L363-369)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L382-384)
```text
        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
```

**File:** src/Midnight.sol (L391-391)
```text
        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

**File:** src/Midnight.sol (L408-417)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);

        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
```

**File:** src/Midnight.sol (L445-456)
```text
        if (buyerCallback != address(0)) {
            bytes memory buyerCallbackData = offer.buy ? offer.callbackData : takerCallbackData;
            require(
                IBuyCallback(buyerCallback)
                    .onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
                == CALLBACK_SUCCESS,
                WrongBuyCallbackReturnValue()
            );
        }

        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/Midnight.sol (L476-476)
```text
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

**File:** certora/README.md (L42-42)
```markdown
  It never decreases, a take's delta matches the units taken and stays within the offer's max, and once at the max it stops moving: a fully-consumed offer then admits only no-op takes.
```
