Audit Report

## Title
Unprivileged caller can designate an arbitrary contract as repay payer, draining its loan tokens - (File: src/Midnight.sol)

## Summary
In `Midnight.repay`, when a non-zero `callback` address is supplied, `payer` is unconditionally set to `callback` with no check that `callback` is authorized to act as payer. An attacker with any debt can pass a victim contract as `callback`, causing `safeTransferFrom` to pull loan tokens from that contract while reducing only the attacker's own debt. The `onRepay` interface omits the initiating `msg.sender`, so the victim contract cannot distinguish a legitimate self-initiated repay from an attacker-initiated one.

## Finding Description

**Exact code path** — `src/Midnight.sol` lines 502–521:

```solidity
function repay(Market memory market, uint256 units, address onBehalf, address callback, bytes calldata data)
    external
{
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized()); // (1)
    bytes32 id = touchMarket(market);

    position[id][onBehalf].debt -= UtilsLib.toUint128(units);                              // (2)
    marketState[id].withdrawable += UtilsLib.toUint128(units);

    address payer = callback != address(0) ? callback : msg.sender;                        // (3)
    ...
    if (callback != address(0)) {
        require(
            IRepayCallback(callback).onRepay(id, market, units, onBehalf, data)            // (4)
                == CALLBACK_SUCCESS, ...
        );
    }
    SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);       // (5)
}
``` [1](#0-0) 

**Root cause:** Check (1) at line 505 authorizes the caller to modify the `onBehalf` debt position only. Line 511 unconditionally sets `payer = callback` with zero verification that `callback == msg.sender` or that `isAuthorized[callback][msg.sender]` holds. There is no such check anywhere in the function. [2](#0-1) 

**`onRepay` interface omits initiating caller** — `src/interfaces/ICallbacks.sol` line 21:

```solidity
function onRepay(bytes32 id, Market memory market, uint256 units, address onBehalf, bytes memory data) external returns (bytes32);
``` [3](#0-2) 

Compare with `onLiquidate` (line 17) and `onFlashLoan` (line 25), which both include `address caller` as their first parameter. `onRepay` is the only payment-bearing callback that omits it, making it impossible for a victim contract to identify the initiating caller. [4](#0-3) 

**Exploit flow:**
1. Attacker holds `position[id][attacker].debt > 0` in some market.
2. `victimContract` satisfies: (a) `loanToken.allowance(victimContract, midnight) >= units`, and (b) `IRepayCallback(victimContract).onRepay(...) == CALLBACK_SUCCESS`.
3. Attacker calls `repay(market, units, attacker, victimContract, data)`.
4. Check (1) passes trivially (`onBehalf == msg.sender`).
5. `position[id][attacker].debt` decreases by `units`.
6. `payer` is set to `victimContract`.
7. `onRepay` is called on `victimContract`; it returns `CALLBACK_SUCCESS` — it has no way to identify the initiating caller.
8. `safeTransferFrom(loanToken, victimContract, midnight, units)` executes — `units` loan tokens are pulled from `victimContract`.

**Why existing checks fail:**

- The `isAuthorized` check at line 505 guards the debt position owner, not the payer. [5](#0-4) 
- There is no `require(callback == msg.sender || isAuthorized[callback][msg.sender])` anywhere in `repay`.
- The `RepayCallback` test contract (`test/OtherFunctionsTest.sol` lines 723–734) checks `marketId == IdLib.toId(market, block.chainid, msg.sender)` where `msg.sender` is Midnight — this validates the market ID but not the initiating caller, so it does not block the exploit. [6](#0-5) 
- The Certora spec `certora/specs/OnlyExplicitPayerCanLoseTokens.spec` (lines 119–135) sets `repayCallbackAllowed = true` for the `repay` selector, proving tokens are only pulled from `msg.sender` or a callback that returned `CALLBACK_SUCCESS`. This invariant is technically satisfied during the attack (the victim did return `CALLBACK_SUCCESS`), so the spec does not detect the vulnerability. [7](#0-6) 

## Impact Explanation
The attacker's debt (`position[id][attacker].debt`) decreases by `units` while `victimContract`'s loan token balance decreases by `units`. The attacker fully repays their own debt at zero personal cost. This is direct, repeatable theft of loan tokens from any qualifying victim contract, violating the invariant that the repaying party must supply the loan tokens. Impact: **Critical** — direct theft of user/protocol funds.

## Likelihood Explanation
Three preconditions are required:
1. Victim implements `IRepayCallback.onRepay` returning `CALLBACK_SUCCESS` — satisfied by any contract designed to participate in Midnight's repay-callback flow (aggregators, routers, helper contracts).
2. Victim has a non-zero `loanToken` approval to Midnight — satisfied by any contract that calls `forceApproveMax` for loan tokens or grants an unlimited approval. `MidnightBundles` (`src/periphery/MidnightBundles.sol`) calls `forceApproveMax(loanToken, MIDNIGHT)` in multiple bundle functions. [8](#0-7) 
3. Attacker has any non-zero debt in the market — trivially achievable.

All three preconditions are realistic in normal protocol operation. The attack is permissionless, requires no special role, and is repeatable across any qualifying victim.

## Recommendation
Add an authorization check on `callback` before using it as `payer`. The simplest fix mirrors the existing `onBehalf` guard:

```solidity
if (callback != address(0)) {
    require(callback == msg.sender || isAuthorized[callback][msg.sender], Unauthorized());
}
address payer = callback != address(0) ? callback : msg.sender;
```

Additionally, add `address caller` as the first parameter to `IRepayCallback.onRepay` (matching `onLiquidate` and `onFlashLoan`) so that callback implementors can independently verify the initiating caller.

## Proof of Concept
Minimal Foundry test:
1. Deploy a market with a loan token.
2. Deploy `VictimCallback` implementing `onRepay` returning `CALLBACK_SUCCESS`, with `type(uint256).max` approval to Midnight for the loan token, and funded with `units` loan tokens.
3. Attacker borrows `units` debt in the market.
4. Attacker calls `midnight.repay(market, units, attacker, address(victimCallback), "")`.
5. Assert: `attacker`'s debt is 0, `victimCallback`'s loan token balance decreased by `units`, attacker paid nothing.

### Citations

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

**File:** src/interfaces/ICallbacks.sol (L16-26)
```text
interface ILiquidateCallback {
    function onLiquidate(address caller, bytes32 id, Market memory market, uint256 collateralIndex, uint256 seizedAssets, uint256 repaidUnits, address borrower, address receiver, bytes memory data, uint256 badDebt) external returns (bytes32);
}

interface IRepayCallback {
    function onRepay(bytes32 id, Market memory market, uint256 units, address onBehalf, bytes memory data) external returns (bytes32);
}

interface IFlashLoanCallback {
    function onFlashLoan(address caller, address[] memory tokens, uint256[] memory assets, bytes memory data) external returns (bytes32);
}
```

**File:** test/OtherFunctionsTest.sol (L723-734)
```text
    function onRepay(bytes32 marketId, Market memory market, uint256 units, address onBehalf, bytes memory data)
        external
        returns (bytes32)
    {
        require(marketId == IdLib.toId(market, block.chainid, msg.sender), "wrong marketId");
        recordedId = marketId;
        _recordedMarket = market;
        recordedData = data;
        recordedUnits = units;
        recordedOnBehalf = onBehalf;
        return CALLBACK_SUCCESS;
    }
```

**File:** certora/specs/OnlyExplicitPayerCanLoseTokens.spec (L119-135)
```text
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

**File:** src/periphery/MidnightBundles.sol (L371-375)
```text
    function forceApproveMax(address token, address spender) internal {
        if (IERC20(token).allowance(address(this), spender) >= type(uint96).max / 2) return;
        safeApprove(token, spender, 0);
        safeApprove(token, spender, type(uint256).max);
    }
```
