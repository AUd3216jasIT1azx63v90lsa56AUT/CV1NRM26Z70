Audit Report

## Title
Liquidator Gate Bypassed via Repay Callback Reentrancy - (File: src/Midnight.sol)

## Summary
The `repay` function invokes a caller-supplied callback mid-execution with no reentrancy guard and no liquidation lock. A blocked address can supply a gate-whitelisted contract as the callback, which then calls `liquidate` with `msg.sender` equal to the whitelisted contract, causing the gate check to pass and fully bypassing the liquidator gate invariant. Any unhealthy borrower in a gated market can be liquidated by an unauthorized party.

## Finding Description

**Root cause:** `repay` (lines 502–521) performs no gate check and sets no reentrancy lock before invoking the caller-supplied callback:

```solidity
if (callback != address(0)) {
    require(
        IRepayCallback(callback).onRepay(id, market, units, onBehalf, data) == CALLBACK_SUCCESS,
        WrongRepayCallbackReturnValue()
    );
}
```

There is no `nonReentrant` modifier or equivalent guard anywhere in `src/Midnight.sol` (confirmed by code search). Inside the callback, any call to `liquidate` has `msg.sender == callback`. The gate check in `liquidate` (lines 597–600) evaluates `msg.sender`:

```solidity
require(
    market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
    LiquidatorGatedFromLiquidating()
);
```

Because `msg.sender` is the callback contract (not the original `repay` caller), the gate evaluates the callback's address, not the blocked address.

**Why `liquidationLocked` does not protect here:** The `liquidationLocked` guard (lines 620–624) is keyed on the *borrower* being liquidated, not on the caller. It is only written during `take` callbacks for the seller (line 444 via `UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, true)`). It is never set during `repay` callbacks, so it provides zero protection against this path.

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

**Certora coverage gap:** The rule `liquidatorGateBlocksLiquidation` (Reverts.spec lines 325–332) models `liquidate` called directly with `e.msg.sender` as the top-level caller. It does not model the nested-call path from a `repay` callback, so this path is not covered by formal verification.

## Impact Explanation
The liquidator gate invariant — that only permitted addresses may liquidate in a gated market — is fully violated. A blocked address can liquidate any unhealthy borrower in the market, seizing collateral and repaying debt at the liquidation incentive factor. This constitutes unauthorized movement of assets (seized collateral) and unauthorized state changes (borrower debt/collateral reduction, bad debt socialization) that the gate was designed to prevent. This is a direct theft/unauthorized movement of assets impact.

## Likelihood Explanation
Preconditions: (1) a market with a non-zero `liquidatorGate` that distinguishes between specific addresses or address types; (2) the attacker can deploy or control a contract that the gate allows. Both are realistic: permissioned markets commonly whitelist smart-contract liquidators (MEV bots, liquidation routers) while blocking EOAs, or whitelist specific addresses while blocking others. The attack requires no special privilege, no oracle manipulation, no flash loan, and no victim mistake. It is repeatable on every unhealthy position in the market.

## Recommendation
Add a reentrancy guard to `repay` (and all other callback-invoking functions) that prevents re-entry into `liquidate`. The most targeted fix is to record the caller's identity before the callback and propagate it through to the gate check, or to set a transient lock during `repay` callbacks that causes `liquidate` to revert. A global `nonReentrant` modifier on all external state-mutating functions would also close this and any analogous paths (e.g., via `flashLoan` callbacks).

## Proof of Concept

**Minimal test setup:**
1. Deploy a `MockLiquidatorGate` that returns `true` for contract B and `false` for EOA A.
2. Create a market with `liquidatorGate = address(MockLiquidatorGate)`.
3. Supply collateral and borrow as `victimBorrower`; move oracle price to make position unhealthy.
4. Deploy contract B implementing `IRepayCallback.onRepay` which calls `midnight.liquidate(market, 0, seizedAssets, 0, victimBorrower, false, A, address(0), "")`.
5. As EOA A, call `midnight.repay(market, 0, A, address(B), "")`.
6. Observe: liquidation succeeds, collateral transferred to A, despite A being blocked by the gate.

**Expected revert (if fixed):** `LiquidatorGatedFromLiquidating()` when A calls `liquidate` directly.
**Actual result (unfixed):** Liquidation succeeds via the callback path.