Audit Report

## Title
Unchecked `callback` payer designation in `repay()` enables theft of loanToken from victim contracts via `onSell` re-entrancy - (File: src/Midnight.sol)

## Summary
`repay()` enforces that `msg.sender` is authorized to act on behalf of `onBehalf` (the borrower) but imposes no authorization check on the `callback` parameter, which becomes the payer and receives the `onRepay` call. A malicious seller callback contract can, from within the `onSell` window of `take()`, call `repay(market, units, seller, callback=victim, data)` to designate an arbitrary victim contract as the payer, pulling victim's loanToken into Midnight to retire the seller's debt at the victim's expense.

## Finding Description

**Root cause — missing payer authorization in `repay()`:** [1](#0-0) 

The sole gate at line 505 is `isAuthorized[onBehalf][msg.sender]`, verifying the right to repay on behalf of the borrower. There is no check that `callback` (set as `payer` at line 511) has authorized `msg.sender` to use it as a payer or to invoke `onRepay` on it. Tokens are pulled from `payer` (= `callback`) at line 520 after `onRepay` returns `CALLBACK_SUCCESS`.

**Re-entrancy window in `take()` — `onSell` callback:** [2](#0-1) 

The liquidation lock set at line 444 only prevents the seller from being liquidated during the callback window. It does not restrict what the `onSell` callback may do to third parties, and there is no reentrancy guard on `repay()`. The `onSell` callback is invoked at lines 458–473 after the buyer-side token pulls (lines 455–456). The post-callback health check at line 476 passes if the seller's debt was repaid inside the callback.

**Exploit flow:**

1. Attacker deploys `maliciousContract` and calls `setIsAuthorized(maliciousContract, true, seller)` — standard precondition for any seller callback that acts on the seller's behalf.
2. Seller creates a sell offer (`offer.buy = false`) with `offer.callback = maliciousContract`.
3. Taker calls `take(offer, ...)`. `sellerCallback = offer.callback = maliciousContract` (line 421).
4. After buyer-side token pulls (lines 455–456), `onSell` is invoked on `maliciousContract` (lines 458–473).
5. Inside `onSell`, `maliciousContract` calls `repay(market, units, seller, callback=victim, data)`:
   - `msg.sender = maliciousContract`; `isAuthorized[seller][maliciousContract] = true` → line 505 passes.
   - `payer = victim` (line 511).
   - `IRepayCallback(victim).onRepay(id, market, units, seller, data)` is called; if victim returns `CALLBACK_SUCCESS`, execution continues.
   - `safeTransferFrom(loanToken, victim, midnight, units)` pulls `units` loanToken from victim (line 520).
6. Seller's debt is reduced (line 508). The health check at line 476 passes because the debt was repaid.

**Why existing checks fail:**

- The `repay` authorization check (line 505) only verifies the right to repay on behalf of the seller; it says nothing about who may be designated as payer.
- The liquidation lock (line 444) prevents the seller from being liquidated during the callback window but does not restrict what the callback may do to third parties.
- There is no reentrancy guard on `repay()`.
- The Certora spec `OnlyExplicitPayerCanLoseTokens.spec` sets `repayCallbackAllowed = false` for `take` (line 108) and models `onSell` with `HAVOC_ALL` under the stated assumption (lines 13–14) that "onSell cannot authorize a payer." This assumption is incorrect: `onSell` can re-enter `repay()` with an arbitrary `callback`, and the `HAVOC_ALL` abstraction does not trace through the re-entrant call to `repay()` → `onRepay()` → `transferFrom(victim)`, so the prover cannot detect this path. [3](#0-2) [4](#0-3) 

## Impact Explanation

Victim loses loanToken equal to `units` (the repaid amount). The seller's debt is retired at the victim's expense while the seller retains collateral and is protected from liquidation throughout the window. This is a direct, concrete theft of assets from any Midnight-integrated contract that implements `IRepayCallback`, holds a loanToken balance, and has approved Midnight — all standard properties of repay-callback contracts. The attack is repeatable as long as the victim contract remains deployed and approved.

## Likelihood Explanation

**Required preconditions:**
1. Seller authorizes their callback contract — standard practice for any callback that calls `supplyCollateral` or similar on the seller's behalf.
2. Victim contract implements `IRepayCallback.onRepay` returning `CALLBACK_SUCCESS` without validating `onBehalf` — realistic for aggregators, vaults, or any Midnight-integrated contract that uses repay callbacks for its own operations and does not defensively check the `onBehalf` parameter.
3. Victim has approved Midnight to spend loanToken and holds a loanToken balance — true for any active repay-callback contract.

All three preconditions are reachable by an unprivileged attacker who controls the seller account and callback contract. The attack requires no privileged keys, no oracle manipulation, and no external dependencies beyond a victim contract meeting the above criteria.

## Recommendation

Add an explicit authorization check in `repay()` requiring that `callback` has authorized `msg.sender` to use it as a payer, mirroring the existing `onBehalf` authorization pattern:

```solidity
if (callback != address(0)) {
    require(
        callback == msg.sender || isAuthorized[callback][msg.sender],
        UnauthorizedPayer()
    );
}
```

Alternatively, restrict `callback` to only be `msg.sender` itself (i.e., the caller funds the repayment via its own callback), eliminating the ability to designate arbitrary third-party payers. Additionally, update the Certora spec to correctly model `onSell` as capable of re-entering `repay()` with an arbitrary `callback`, removing the incorrect assumption at lines 13–14.

## Proof of Concept

**Minimal Foundry test outline:**

1. Deploy `MaliciousContract` that implements `ISellCallback.onSell`. Inside `onSell`, it calls `midnight.repay(market, units, seller, address(victimContract), data)`.
2. Deploy `VictimContract` that implements `IRepayCallback.onRepay` returning `CALLBACK_SUCCESS` without checking `onBehalf`. Pre-fund it with loanToken and approve Midnight.
3. Seller calls `midnight.setIsAuthorized(address(maliciousContract), true, seller)`.
4. Seller creates a sell offer with `offer.callback = address(maliciousContract)`.
5. Taker calls `midnight.take(offer, ...)`.
6. Assert: `victimContract`'s loanToken balance decreased by `units`; seller's debt in Midnight decreased by `units`.

Expected result: victim's tokens are pulled into Midnight, seller's debt is retired, no revert occurs.

### Citations

**File:** src/Midnight.sol (L444-476)
```text
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

        if (sellerCallback != address(0)) {
            bytes memory sellerCallbackData = offer.buy ? takerCallbackData : offer.callbackData;
            require(
                ISellCallback(sellerCallback)
                    .onSell(
                        id,
                        offer.market,
                        sellerAssets,
                        units,
                        sellerPendingFeeDecrease,
                        seller,
                        receiver,
                        sellerCallbackData
                    ) == CALLBACK_SUCCESS,
                WrongSellCallbackReturnValue()
            );
        }
        if (!wasLocked) UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, false);
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

**File:** src/Midnight.sol (L502-521)
```text
    function repay(Market memory market, uint256 units, address onBehalf, address callback, bytes calldata data)
        external
    {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);

        position[id][onBehalf].debt -= UtilsLib.toUint128(units);
        marketState[id].withdrawable += UtilsLib.toUint128(units);

        address payer = callback != address(0) ? callback : msg.sender;
        emit EventsLib.Repay(msg.sender, id, units, onBehalf, payer);

        if (callback != address(0)) {
            require(
                IRepayCallback(callback).onRepay(id, market, units, onBehalf, data) == CALLBACK_SUCCESS,
                WrongRepayCallbackReturnValue()
            );
        }
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
    }
```

**File:** certora/specs/OnlyExplicitPayerCanLoseTokens.spec (L11-18)
```text
    // Callbacks can modify the whole state arbitrarily, and can only modify the ghost variables to allow
    // themselves as payer. Callbacks are checked to only be called by their corresponding function,
    // eg onLiquidate is only called by liquidate. onRatify and onSell cannot authorize a payer, so we
    // model them with a plain HAVOC_ALL.
    function _.onBuy(bytes32, Midnight.Market, uint256, uint256, uint256, address, bytes) external => onCallBackSummary(calledContract, buyCallbackAllowed) expect(bytes32);
    function _.onLiquidate(address, bytes32, Midnight.Market, uint256, uint256, uint256, address, address, bytes, uint256) external => onCallBackSummary(calledContract, liquidateCallbackAllowed) expect(bytes32);
    function _.onRepay(bytes32, Midnight.Market, uint256, address, bytes) external => onCallBackSummary(calledContract, repayCallbackAllowed) expect(bytes32);
    function _.onFlashLoan(address, address[], uint256[], bytes) external => onCallBackSummary(calledContract, flashLoanCallbackAllowed) expect(bytes32);
```

**File:** certora/specs/OnlyExplicitPayerCanLoseTokens.spec (L106-114)
```text
    buyCallbackAllowed = true;
    liquidateCallbackAllowed = false;
    repayCallbackAllowed = false;
    flashLoanCallbackAllowed = false;
    badPullSeen = false;

    take(e, offer, ratifierData, units, taker, receiverIfTakerIsSeller, takerCallback, takerCallbackData);

    assert !badPullSeen;
```
