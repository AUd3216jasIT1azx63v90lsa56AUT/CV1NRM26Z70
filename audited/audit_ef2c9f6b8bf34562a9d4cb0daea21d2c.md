Audit Report

## Title
Callback Address Used as Payer Without Caller Authorization, Enabling Theft of Loan Tokens from Contracts Implementing `onLiquidate` - (File: src/Midnight.sol)

## Summary
In `liquidate()`, `payer` is unconditionally set to `callback` at line 679 when `callback != address(0)`, with no requirement that `callback` authorized `msg.sender` to use it as payer. Any contract implementing `ILiquidateCallback` that returns `CALLBACK_SUCCESS` without verifying it initiated the call — and holds a Midnight approval for the loan token — can be drained by an unprivileged attacker who passes that contract's address as `callback` while directing seized collateral to themselves via `receiver`.

## Finding Description
**Root cause — `src/Midnight.sol` lines 679 and 717:**

```solidity
address payer = callback != address(0) ? callback : msg.sender;  // line 679
```
```solidity
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits); // line 717
```

The only gate between the `payer = callback` assignment and the token pull is the `onLiquidate` return-value check at lines 698–714. Returning `CALLBACK_SUCCESS` is treated as implicit consent to pay `repaidUnits` of loan token. There is no check that `callback == msg.sender`, that `callback` authorized `msg.sender` to use it as payer, or that `callback` initiated the liquidation.

The `onLiquidate` callback receives `msg.sender` (the attacker) as the `_caller` argument. A callback implementer checking only `msg.sender == address(midnight)` (the standard reentrancy guard) will pass this check, because the attacker IS calling through Midnight. The only protection would be checking `_caller == address(this)`, which is non-obvious and undocumented.

The reference test implementation at `test/LiquidationTest.sol` lines 985–1009 demonstrates the vulnerable pattern: it records `_caller` but does not assert `_caller == address(this)`, and returns `CALLBACK_SUCCESS` unconditionally.

The `liquidatorGate` check at lines 597–600 only gates whether `msg.sender` can liquidate at all; it does not constrain the `callback` parameter.

**Exploit flow:**

1. Victim contract `V` implements `onLiquidate`, checks `msg.sender == address(midnight)` but not `_caller == address(this)`, and has approved Midnight for the loan token.
2. Attacker identifies a liquidatable borrower `B`.
3. Attacker calls:
   ```solidity
   midnight.liquidate(market, idx, seizedAssets, 0, B, false, attacker, V, "");
   ```
4. Execution:
   - Line 679: `payer = V`
   - Line 696: collateral transferred to attacker
   - Lines 698–714: `V.onLiquidate(attacker, ...)` called; `msg.sender` is Midnight (passes guard); `V` returns `CALLBACK_SUCCESS`
   - Line 717: `safeTransferFrom(loanToken, V, address(this), repaidUnits)` — loan tokens pulled from `V`
5. Net result: attacker gains seized collateral at zero personal cost; `V` loses `repaidUnits` of loan token.

## Impact Explanation
Direct theft of loan tokens from any contract that (a) implements `ILiquidateCallback` returning `CALLBACK_SUCCESS` without verifying `_caller == address(this)` and (b) holds a Midnight approval for the loan token. The attacker simultaneously receives seized collateral at no personal cost. The loss is concrete, repeatable, and constitutes unauthorized movement of assets outside valid protocol rules. Severity: Critical.

## Likelihood Explanation
All three preconditions are simultaneously satisfied by the primary intended users of the callback feature — flash-liquidation bots and protocol integrations that implement `onLiquidate` for their own liquidations and pre-approve Midnight for loan tokens. The `_caller` check is non-obvious: the protocol does not document it as a required security invariant, and the reference test implementation omits it. A liquidatable borrower is a normal market condition. The attack requires no privileged access and is executable by any external user.

## Recommendation
Require that `callback == msg.sender` when `callback != address(0)`:

```solidity
if (callback != address(0)) {
    require(callback == msg.sender, UnauthorizedCallback());
}
address payer = callback != address(0) ? callback : msg.sender;
```

This ensures only the caller can designate itself as the callback payer, consistent with the authorization model used elsewhere in the protocol (e.g., `repay()` line 505, `supplyCollateral()` line 527). Alternatively, document `_caller == address(this)` as a mandatory security invariant in the `ILiquidateCallback` interface and enforce it in the reference implementation.

## Proof of Concept
Minimal Foundry test:

```solidity
// AttackerTest.sol
function testStealFromCallbackBot() public {
    // Setup: bot has approved Midnight for loanToken, implements onLiquidate
    // returning CALLBACK_SUCCESS without checking _caller == address(bot)
    address bot = address(new VulnerableBot(midnight, loanToken));
    deal(loanToken, bot, repaidUnits);
    vm.prank(bot);
    IERC20(loanToken).approve(address(midnight), type(uint256).max);

    // Attacker has no loan tokens
    uint256 attackerLoanBefore = IERC20(loanToken).balanceOf(attacker);
    uint256 botLoanBefore = IERC20(loanToken).balanceOf(bot);

    // Attacker liquidates borrower B, passing bot as callback, self as receiver
    vm.prank(attacker);
    midnight.liquidate(market, collateralIndex, seizedAssets, 0, B, false, attacker, bot, "");

    // Attacker gained collateral, bot lost loan tokens
    assertGt(IERC20(collateralToken).balanceOf(attacker), 0);
    assertLt(IERC20(loanToken).balanceOf(bot), botLoanBefore);
    assertEq(IERC20(loanToken).balanceOf(attacker), attackerLoanBefore); // attacker paid nothing
}
```