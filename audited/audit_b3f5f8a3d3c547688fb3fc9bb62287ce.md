Audit Report

## Title
Liquidator Gate Bypassed via Repay Callback Reentrancy - (File: src/Midnight.sol)

## Summary
The `repay` function invokes a caller-supplied callback mid-execution with no reentrancy guard and no liquidation-caller lock. A blocked address can supply a gate-whitelisted contract as the callback, which then calls `liquidate` with `msg.sender` equal to the whitelisted contract, causing the gate check to pass and fully bypassing the liquidator gate invariant. Any unhealthy borrower in a gated market can be liquidated by an unauthorized party.

## Finding Description

**Root cause:** `repay` (lines 502–521) performs no gate check and sets no reentrancy lock before invoking the caller-supplied callback: [1](#0-0) 

There is no `nonReentrant` modifier or equivalent guard anywhere in `src/Midnight.sol` (confirmed by code search — zero matches for `nonReentrant`, `ReentrancyGuard`, `_locked`, `_entered`).

Inside the callback, any call to `liquidate` has `msg.sender == callback`. The gate check in `liquidate` evaluates `msg.sender`: [2](#0-1) 

Because `msg.sender` is the callback contract (not the original `repay` caller), the gate evaluates the callback's address, not the blocked address.

**Why `liquidationLocked` does not protect here:** The `liquidationLocked` guard is keyed on the *borrower* being liquidated, not on the caller: [3](#0-2) 

It is only written during `take` callbacks for the seller: [4](#0-3) 

It is never set during `repay` callbacks, so it provides zero protection against this path.

**Why `units=0` is valid:** Line 508 performs `position[id][onBehalf].debt -= UtilsLib.toUint128(units)`. With `units=0`, this is `uint128(0) - uint128(0) = 0` — no underflow in Solidity 0.8+, no revert. A need not hold any debt. [5](#0-4) 

**Exploit flow:**
1. Market is created with a `liquidatorGate` that blocks address A but allows contract B.
2. A calls `midnight.repay(market, 0, A, B, encodedLiquidateArgs)`.
   - Authorization: `onBehalf == A == msg.sender` → passes.
   - `position[id][A].debt -= 0` → no underflow; A need not hold any debt.
   - `marketState[id].withdrawable += 0` → no-op.
   - Callback is invoked because `callback != address(0)`.
3. `repay` calls `B.onRepay(id, market, 0, A, encodedLiquidateArgs)`.
4. Inside `B.onRepay`, B calls `midnight.liquidate(market, collateralIndex, seizedAssets, 0, victimBorrower, false, receiver, address(0), "")`.
5. Inside `liquidate`, `msg.sender == B`; gate returns `true` for B → gate check passes.
6. `victimBorrower` (who has `debt > 0`, satisfying the `NotBorrower` check at line 596) is liquidated; seized collateral is sent to `receiver` (controlled by A).

**Certora coverage gap:** The rule `liquidatorGateBlocksLiquidation` models `liquidate` called directly with `e.msg.sender` as the top-level caller. It does not model the nested-call path from a `repay` callback, so this path is not covered by formal verification.

## Impact Explanation
The liquidator gate invariant — that only permitted addresses may liquidate in a gated market — is fully violated. A blocked address can liquidate any unhealthy borrower in the market, seizing collateral and repaying debt at the liquidation incentive factor. This constitutes unauthorized movement of assets (seized collateral) and unauthorized state changes (borrower debt/collateral reduction, bad debt socialization) that the gate was designed to prevent.

## Likelihood Explanation
Preconditions: (1) a market with a non-zero `liquidatorGate` that distinguishes between specific addresses or address types; (2) the attacker can deploy or control a contract that the gate allows. Both are realistic: permissioned markets commonly whitelist smart-contract liquidators (MEV bots, liquidation routers) while blocking EOAs, or whitelist specific addresses while blocking others. The attack requires no special privilege, no oracle manipulation, no flash loan, and no victim mistake. It is repeatable on every unhealthy position in the market.

## Recommendation
Add a reentrancy lock that covers the cross-function reentrant path. The most targeted fix is to set a transient caller-keyed lock at the start of `repay` (and clear it after) and check it at the start of `liquidate`. Alternatively, add a global transient reentrancy guard (`nonReentrant`) across all state-mutating external functions in `Midnight.sol`. A secondary defense is to record the original `msg.sender` before the callback and pass it through for gate evaluation, but the reentrancy guard is the correct structural fix.

## Proof of Concept
1. Deploy a market with a `liquidatorGate` that returns `canLiquidate(B) = true` and `canLiquidate(A) = false`.
2. Create an unhealthy borrower position for `victimBorrower`.
3. Deploy contract B implementing `IRepayCallback`. In `onRepay`, B calls `midnight.liquidate(market, 0, seizedAssets, 0, victimBorrower, false, A, address(0), "")`.
4. From address A, call `midnight.repay(market, 0, A, B, abi.encode(...))`.
5. Observe: liquidation succeeds, collateral transferred to A, despite A being blocked by the gate.

Forge test skeleton:
```solidity
function testRepayCallbackBypassesLiquidatorGate() public {
    // Setup: market with gate blocking A, allowing B
    // Setup: unhealthy victimBorrower position
    // B.onRepay calls midnight.liquidate(...)
    vm.prank(A);
    midnight.repay(market, 0, A, address(B), encodedArgs);
    // Assert: victimBorrower collateral reduced, A's receiver received seized assets
}
```

### Citations

**File:** src/Midnight.sol (L444-444)
```text
        bool wasLocked = UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, true);
```

**File:** src/Midnight.sol (L508-509)
```text
        position[id][onBehalf].debt -= UtilsLib.toUint128(units);
        marketState[id].withdrawable += UtilsLib.toUint128(units);
```

**File:** src/Midnight.sol (L514-519)
```text
        if (callback != address(0)) {
            require(
                IRepayCallback(callback).onRepay(id, market, units, onBehalf, data) == CALLBACK_SUCCESS,
                WrongRepayCallbackReturnValue()
            );
        }
```

**File:** src/Midnight.sol (L597-600)
```text
        require(
            market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
            LiquidatorGatedFromLiquidating()
        );
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
```
