Audit Report

## Title
Unprivileged caller can designate an arbitrary contract as repay payer, draining its loan tokens - (File: src/Midnight.sol)

## Summary
In `Midnight.repay`, the `payer` is unconditionally set to `callback` when a non-zero callback address is supplied, with no check that `callback` is authorized to act as payer. An attacker holding any debt can pass a victim contract as `callback`, causing `safeTransferFrom` to pull loan tokens from that contract while reducing only the attacker's own debt. The `onRepay` interface omits the initiating `msg.sender`, so the victim contract cannot distinguish a legitimate self-initiated repay from an attacker-initiated one.

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
```

**Root cause:** Check (1) authorizes the caller to modify the `onBehalf` debt position only. Line (3) unconditionally sets `payer = callback` with zero verification that `callback == msg.sender` or that `isAuthorized[callback][msg.sender]` holds. There is no such check anywhere in the function.

**`onRepay` interface** (`src/interfaces/ICallbacks.sol` line 21):
```solidity
function onRepay(bytes32 id, Market memory market, uint256 units, address onBehalf, bytes memory data) external returns (bytes32);
```
The initiating `msg.sender` (the attacker) is **not** passed. Compare with `onLiquidate` and `onFlashLoan`, which both include `address caller` as their first parameter — `onRepay` is the only payment-bearing callback that omits it.

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
- The `isAuthorized` check at line 505 guards the debt position owner, not the payer.
- There is no `require(callback == msg.sender || isAuthorized[callback][msg.sender])` anywhere.
- The `RepayCallback` test contract (`test/OtherFunctionsTest.sol` lines 723–734) checks `marketId == IdLib.toId(market, block.chainid, msg.sender)` where `msg.sender` is Midnight — this validates the market ID but not the initiating caller, so it does not block the exploit.
- The Certora spec `certora/specs/OnlyExplicitPayerCanLoseTokens.spec` (lines 119–135) proves tokens are only pulled from `msg.sender` or a callback that returned `CALLBACK_SUCCESS`. This invariant is technically satisfied during the attack (the victim did return `CALLBACK_SUCCESS`), so the spec does not detect the vulnerability.

## Impact Explanation
The attacker's debt (`position[id][attacker].debt`) decreases by `units` while `victimContract`'s loan token balance decreases by `units`. The attacker fully repays their own debt at zero personal cost. This is direct, repeatable theft of loan tokens from any qualifying victim contract, violating the invariant that the repaying party must supply the loan tokens. Impact: **Critical** — direct theft of user/protocol funds.

## Likelihood Explanation
Three preconditions are required:
1. Victim implements `IRepayCallback.onRepay` returning `CALLBACK_SUCCESS` — satisfied by any contract designed to participate in Midnight's repay-callback flow (aggregators, routers, helper contracts).
2. Victim has a non-zero `loanToken` approval to Midnight — satisfied by any contract that calls `forceApproveMax` for loan tokens or grants an unlimited approval.
3. Attacker has any non-zero debt in the market — trivially achievable.

All three preconditions are realistic in normal protocol operation. The attack is permissionless, requires no special role, and is repeatable across any qualifying victim. `MidnightBundles` (`src/periphery/MidnightBundles.sol`) is a concrete candidate given its use of `forceApproveMax` and its callback implementations.

## Recommendation
Add an authorization check on `callback` before using it as payer:

```solidity
if (callback != address(0)) {
    require(callback == msg.sender || isAuthorized[callback][msg.sender], Unauthorized());
}
```

Alternatively, pass the initiating `msg.sender` as the first argument to `onRepay` (consistent with `onLiquidate` and `onFlashLoan`), so the callback can verify consent before returning `CALLBACK_SUCCESS`. Both fixes should be applied together for defense in depth.

## Proof of Concept

**Minimal Foundry test:**
1. Deploy Midnight and a market with `loanToken = TOKEN`.
2. Deploy `VictimCallback` that implements `onRepay` returning `CALLBACK_SUCCESS` and holds `TOKEN` with `TOKEN.approve(midnight, type(uint256).max)`.
3. Attacker borrows `units` of `TOKEN` (establishing debt).
4. Attacker calls `midnight.repay(market, units, attacker, address(victimCallback), "")`.
5. Assert: `attacker`'s debt is 0; `victimCallback`'s `TOKEN` balance decreased by `units`; `attacker` never transferred any `TOKEN`.

The existing `RepayCallback` in `test/OtherFunctionsTest.sol` (lines 716–734) already satisfies both preconditions (it approves Midnight at line 719 and returns `CALLBACK_SUCCESS` at line 733) and can be used directly as the victim in a PoC by having a separate attacker address initiate the `repay` call. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** src/interfaces/ICallbacks.sol (L20-22)
```text
interface IRepayCallback {
    function onRepay(bytes32 id, Market memory market, uint256 units, address onBehalf, bytes memory data) external returns (bytes32);
}
```

**File:** test/OtherFunctionsTest.sol (L716-734)
```text
    function repay(Midnight midnight, Market memory market, uint256 units, address onBehalf, bytes memory data)
        external
    {
        ERC20(market.loanToken).approve(address(midnight), units);
        midnight.repay(market, units, onBehalf, address(this), data);
    }

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
