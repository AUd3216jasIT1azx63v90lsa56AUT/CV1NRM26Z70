The code confirms the claim exactly. All referenced lines match:

- `src/Midnight.sol` line 511: `address payer = callback != address(0) ? callback : msg.sender;` — no authorization check on `callback` as payer. [1](#0-0) 
- `src/interfaces/ICallbacks.sol` line 21: `onRepay` omits `address caller`, unlike `onLiquidate` (line 17) and `onFlashLoan` (line 25) which both include it. [2](#0-1) 
- `test/OtherFunctionsTest.sol` line 727: the test callback checks `msg.sender` (which is Midnight, not the initiating caller), confirming victim contracts cannot identify the attacker. [3](#0-2) 
- `certora/specs/OnlyExplicitPayerCanLoseTokens.spec` lines 80–81: the spec allows a pull from `callback` if it returned `CALLBACK_SUCCESS` — technically satisfied during the attack, so the spec does not catch this. [4](#0-3) 

---

Audit Report

## Title
Unprivileged caller can designate an arbitrary contract as repay payer, draining its loan tokens - (File: src/Midnight.sol)

## Summary
In `Midnight.repay`, when a non-zero `callback` is supplied, the `payer` is unconditionally set to `callback` with no check that `callback` is authorized to act as payer on behalf of the caller. An attacker with any outstanding debt can pass a victim contract as `callback`, causing `safeTransferFrom` to pull loan tokens from that contract while reducing only the attacker's own debt. Because `IRepayCallback.onRepay` omits the initiating `msg.sender`, victim contracts cannot distinguish a legitimate self-initiated repay from an attacker-initiated one.

## Finding Description

**Exact code path — `src/Midnight.sol` lines 502–521:**

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

**`onRepay` interface (`src/interfaces/ICallbacks.sol` line 21):**
```solidity
function onRepay(bytes32 id, Market memory market, uint256 units, address onBehalf, bytes memory data) external returns (bytes32);
```
The initiating `msg.sender` (the attacker) is not passed. `onLiquidate` and `onFlashLoan` both include `address caller` as their first parameter — `onRepay` is the only payment-bearing callback that omits it.

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
- The `RepayCallback` test contract (`test/OtherFunctionsTest.sol` line 727) checks `marketId == IdLib.toId(market, block.chainid, msg.sender)` where `msg.sender` is Midnight — this validates the market ID but not the initiating caller, so it does not block the exploit.
- The Certora spec `certora/specs/OnlyExplicitPayerCanLoseTokens.spec` (lines 119–135) proves tokens are only pulled from `msg.sender` or a callback that returned `CALLBACK_SUCCESS`. This invariant is technically satisfied during the attack (the victim did return `CALLBACK_SUCCESS`), so the spec does not detect the vulnerability.

## Impact Explanation
The attacker's debt (`position[id][attacker].debt`) decreases by `units` while `victimContract`'s loan token balance decreases by `units`. The attacker fully repays their own debt at zero personal cost. This is direct, repeatable theft of loan tokens from any qualifying victim contract, violating the invariant that the repaying party must supply the loan tokens. Severity: **Critical** — direct theft of user/protocol funds with no privilege requirement.

## Likelihood Explanation
Three preconditions are required:
1. Victim implements `IRepayCallback.onRepay` returning `CALLBACK_SUCCESS` — satisfied by any contract designed to participate in Midnight's repay-callback flow (aggregators, routers, helper contracts).
2. Victim has a non-zero `loanToken` approval to Midnight — satisfied by any contract that calls `forceApproveMax` for loan tokens or grants an unlimited approval.
3. Attacker has any non-zero debt in the market — trivially achievable by borrowing a minimal amount.

All three preconditions are realistic in normal protocol operation. The attack is permissionless, requires no special role, and is repeatable across any qualifying victim.

## Recommendation
Add an authorization check on `callback` as payer before using it to pull tokens. The simplest fix is to require that `callback` is either the caller or explicitly authorized:

```solidity
if (callback != address(0)) {
    require(callback == msg.sender || isAuthorized[callback][msg.sender], Unauthorized());
}
address payer = callback != address(0) ? callback : msg.sender;
```

Additionally, add `address caller` as the first parameter of `IRepayCallback.onRepay` (consistent with `onLiquidate` and `onFlashLoan`) so that callback implementors can independently verify the initiating caller:

```solidity
function onRepay(address caller, bytes32 id, Market memory market, uint256 units, address onBehalf, bytes memory data) external returns (bytes32);
```

## Proof of Concept
**Minimal Foundry test:**
1. Deploy a market and have the attacker borrow `units` of `loanToken`.
2. Deploy `VictimCallback` that implements `onRepay` returning `CALLBACK_SUCCESS` and holds `units` of `loanToken` with `approve(midnight, type(uint256).max)`.
3. Call `midnight.repay(market, units, attacker, address(victimCallback), "")` from the attacker's address.
4. Assert: `attacker`'s debt is 0, `victimCallback`'s `loanToken` balance decreased by `units`, attacker paid nothing.

### Citations

**File:** src/Midnight.sol (L511-511)
```text
        address payer = callback != address(0) ? callback : msg.sender;
```

**File:** src/interfaces/ICallbacks.sol (L17-21)
```text
    function onLiquidate(address caller, bytes32 id, Market memory market, uint256 collateralIndex, uint256 seizedAssets, uint256 repaidUnits, address borrower, address receiver, bytes memory data, uint256 badDebt) external returns (bytes32);
}

interface IRepayCallback {
    function onRepay(bytes32 id, Market memory market, uint256 units, address onBehalf, bytes memory data) external returns (bytes32);
```

**File:** test/OtherFunctionsTest.sol (L727-727)
```text
        require(marketId == IdLib.toId(market, block.chainid, msg.sender), "wrong marketId");
```

**File:** certora/specs/OnlyExplicitPayerCanLoseTokens.spec (L80-81)
```text
    if (callbackAllowed && src == callback) {
        return true;
```
