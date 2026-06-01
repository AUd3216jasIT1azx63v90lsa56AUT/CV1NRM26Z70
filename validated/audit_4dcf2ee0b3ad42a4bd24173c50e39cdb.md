Audit Report

## Title
Unprivileged liquidator can designate arbitrary third-party contract as `callback`/`payer`, draining its loanToken approval to Midnight - (File: src/Midnight.sol)

## Summary
`Midnight.liquidate` sets `payer = callback` when `callback != address(0)` at line 679 with no requirement that `callback == msg.sender` or that `callback` has authorized `msg.sender`. An attacker can pass any contract implementing `ILiquidateCallback` that returns `CALLBACK_SUCCESS` and holds a loanToken approval to Midnight as the `callback`, causing the protocol to pull `repaidUnits` of loanToken from that victim contract while the attacker's chosen `receiver` collects the seized collateral for free.

## Finding Description
**Confirmed code path:**

`src/Midnight.sol` line 679:
```solidity
address payer = callback != address(0) ? callback : msg.sender;
``` [1](#0-0) 

`src/Midnight.sol` lines 696–717:
```solidity
SafeTransferLib.safeTransfer(collateralToken, receiver, seizedAssets);       // L696
if (callback != address(0)) {
    require(
        ILiquidateCallback(callback).onLiquidate(msg.sender, ...) == CALLBACK_SUCCESS,  // L700-714
        WrongLiquidateCallbackReturnValue()
    );
}
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits); // L717
``` [2](#0-1) 

**Root cause:** No check `require(callback == msg.sender || isAuthorized[callback][msg.sender])` exists anywhere in `liquidate`. The only gate on `callback` is that it must return `CALLBACK_SUCCESS` from `onLiquidate`. [3](#0-2) 

**Exploit flow:**
1. Precondition: `victimContract` implements `ILiquidateCallback`, its `onLiquidate` returns `CALLBACK_SUCCESS` (e.g., only checks `msg.sender == midnight`), and it holds `loanToken.approve(midnight, N)`.
2. Attacker calls: `midnight.liquidate(market, idx, 0, repaidUnits, borrower, false, attackerEOA, victimContract, data)`
3. `payer = victimContract` (line 679)
4. `seizedAssets` of collateral transferred to `attackerEOA` (line 696)
5. `victimContract.onLiquidate(attacker, ...)` called — returns `CALLBACK_SUCCESS` (lines 700–714)
6. `safeTransferFrom(loanToken, victimContract, midnight, repaidUnits)` executes (line 717) — pulls from victim
7. Net: attacker receives collateral worth `repaidUnits * LIF`, pays nothing; victim loses `repaidUnits` of loanToken

**Why existing checks fail:**
- The `liquidatorGate` check (line 598) only gates who can call `liquidate`, not what `callback` is. [4](#0-3) 
- The Certora spec `otherEntryPointsOnlyPullFromCaller` (line 128) sets `liquidateCallbackAllowed = true` for any address passed as callback — it proves only "tokens leave from callback that returned `CALLBACK_SUCCESS`," not "callback must be `msg.sender` or authorized by `msg.sender`." [5](#0-4) 
- The `onCallBackSummary` function asserts `allowedCallback` (which is always `true` for liquidate) and sets `callbackAllowed = true` if `CALLBACK_SUCCESS` is returned — the spec never constrains the identity of `callback` relative to `msg.sender`. [6](#0-5) 

## Impact Explanation
Any contract that (a) implements `ILiquidateCallback` returning `CALLBACK_SUCCESS` and (b) holds a loanToken approval to Midnight can have its entire approved balance drained by an unprivileged attacker in a single `liquidate` call. The attacker receives seized collateral at no personal cost. This is a direct theft of funds from third-party contracts and violates the core invariant that liquidation must not transfer assets from unauthorized parties.

## Likelihood Explanation
**Preconditions:**
1. An unhealthy borrower exists — routine market condition.
2. A victim contract implementing `ILiquidateCallback` with a loanToken approval to Midnight — any deployed liquidation bot or flash-liquidation helper that pre-approves the lending protocol for loanToken satisfies this. Such contracts commonly check only `msg.sender == midnight` in `onLiquidate`, not the `caller` parameter.

**Feasibility:** High. The attacker needs no special privilege, no oracle manipulation, and no governance access. The attack is repeatable as long as the victim's approval persists.

## Recommendation
Add an authorization check before accepting `callback` as payer:
```solidity
require(
    callback == msg.sender || isAuthorized[callback][msg.sender],
    Unauthorized()
);
```
This mirrors the pattern already used in `setConsumed` (line 724) and other functions. [7](#0-6) 

## Proof of Concept
**Minimal Foundry test plan:**
1. Deploy a `VictimCallback` contract that implements `ILiquidateCallback`, returns `CALLBACK_SUCCESS` from `onLiquidate` (checking only `msg.sender == address(midnight)`), and calls `loanToken.approve(address(midnight), type(uint256).max)` in its constructor.
2. Fund `VictimCallback` with loanToken.
3. Create a market, create an undercollateralized borrower position.
4. As attacker EOA (no loanToken balance), call:
   ```solidity
   midnight.liquidate(market, 0, seizedAssets, 0, borrower, false, attackerEOA, address(victimCallback), "");
   ```
5. Assert: attacker's collateral balance increased by `seizedAssets`; `VictimCallback`'s loanToken balance decreased by `repaidUnits`; attacker's loanToken balance unchanged. [1](#0-0)

### Citations

**File:** src/Midnight.sol (L581-600)
```text
    function liquidate(
        Market calldata market,
        uint256 collateralIndex,
        uint256 seizedAssets,
        uint256 repaidUnits,
        address borrower,
        bool postMaturityMode,
        address receiver,
        address callback,
        bytes calldata data
    ) external returns (uint256, uint256) {
        bytes32 id = touchMarket(market);
        MarketState storage _marketState = marketState[id];
        Position storage _position = position[id][borrower];
        require(UtilsLib.atMostOneNonZero(repaidUnits, seizedAssets), InconsistentInput());
        require(_position.debt > 0, NotBorrower()); // to avoid no-op liquidations of non borrower positions.
        require(
            market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
            LiquidatorGatedFromLiquidating()
        );
```

**File:** src/Midnight.sol (L679-679)
```text
        address payer = callback != address(0) ? callback : msg.sender;
```

**File:** src/Midnight.sol (L696-717)
```text
        SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);

        if (callback != address(0)) {
            require(
                ILiquidateCallback(callback)
                    .onLiquidate(
                        msg.sender,
                        id,
                        market,
                        collateralIndex,
                        seizedAssets,
                        repaidUnits,
                        borrower,
                        receiver,
                        data,
                        badDebt
                    ) == CALLBACK_SUCCESS,
                WrongLiquidateCallbackReturnValue()
            );
        }

        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
```

**File:** src/Midnight.sol (L724-724)
```text
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** certora/specs/OnlyExplicitPayerCanLoseTokens.spec (L57-67)
```text
function onCallBackSummary(address callbackAddress, bool allowedCallback) returns (bytes32) {
    assert allowedCallback;
    bytes32 result;
    triggerHavocAll();
    callback = callbackAddress;
    if (result == Utils.callbackSuccess()) {
        assert callbackAllowed == false;
        callbackAllowed = true;
    }
    return result;
}
```

**File:** certora/specs/OnlyExplicitPayerCanLoseTokens.spec (L117-135)
```text
/// Proves that for every entry point other than `take`, tokens are only ever pulled from msg.sender
/// or from a callback that returned CALLBACK_SUCCESS.
rule otherEntryPointsOnlyPullFromCaller(method f, env e, calldataarg args) filtered { f -> !f.isView && f.selector != sig:take(Midnight.Offer, bytes, uint256, address, address, address, bytes).selector } {
    require e.msg.sender != currentContract, "only external calls";

    msgSender = e.msg.sender;
    msgSenderAllowed = true;
    callbackAllowed = false;
    makerAllowed = false;

    buyCallbackAllowed = false;
    liquidateCallbackAllowed = f.selector == sig:liquidate(Midnight.Market, uint256, uint256, uint256, address, bool, address, address, bytes).selector;
    repayCallbackAllowed = f.selector == sig:repay(Midnight.Market, uint256, address, address, bytes).selector;
    flashLoanCallbackAllowed = f.selector == sig:flashLoan(address[], uint256[], address, bytes).selector;
    badPullSeen = false;

    f(e, args);

    assert !badPullSeen;
```
