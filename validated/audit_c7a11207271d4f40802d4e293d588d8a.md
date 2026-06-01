Audit Report

## Title
Callback Address Used as Loan Token Payer Without Authorization, Enabling Theft from Any `onLiquidate` Implementor - (File: src/Midnight.sol)

## Summary
In `liquidate()`, the `payer` for loan token repayment is unconditionally set to `callback` when `callback != address(0)` (line 679), with no check that `callback == msg.sender` or that `callback` authorized `msg.sender` to use it as payer. Any contract implementing `ILiquidateCallback` that returns `CALLBACK_SUCCESS` and holds a standing Midnight approval for the loan token can be drained by an unprivileged attacker who passes that contract's address as `callback` while directing seized collateral to themselves via `receiver`.

## Finding Description
**Root cause — lines 679 and 717 of `src/Midnight.sol`:**

```solidity
// line 679
address payer = callback != address(0) ? callback : msg.sender;

// line 717
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
```

The protocol sets `payer = callback` solely because `callback != address(0)`. The only gate between this assignment and the token pull is the return-value check at lines 698–714:

```solidity
require(
    ILiquidateCallback(callback).onLiquidate(
        msg.sender, id, market, collateralIndex,
        seizedAssets, repaidUnits, borrower, receiver, data, badDebt
    ) == CALLBACK_SUCCESS,
    WrongLiquidateCallbackReturnValue()
);
```

Returning `CALLBACK_SUCCESS` is treated as implicit consent to pay `repaidUnits` of loan token. There is no check that `callback == msg.sender`, that `callback` authorized `msg.sender`, or that `callback` is receiving the collateral.

**Exploit flow:**

1. Victim contract `V` legitimately integrates with Midnight: it implements `onLiquidate` (returns `CALLBACK_SUCCESS`) and holds a standing approval of Midnight for `loanToken`.
2. Attacker identifies a liquidatable borrower `B`.
3. Attacker calls:
   ```solidity
   midnight.liquidate(market, idx, seizedAssets, 0, B, false, attacker, V, "");
   ```
   with `callback = V`, `receiver = attacker`.
4. Execution:
   - Line 679: `payer = V`
   - Line 696: `safeTransfer(collateralToken, attacker, seizedAssets)` — attacker receives collateral
   - Lines 698–714: `V.onLiquidate(attacker, ...)` is called; `V` returns `CALLBACK_SUCCESS` (it cannot distinguish this from a legitimate call it initiated, since Midnight is the `msg.sender` of the callback in both cases)
   - Line 717: `safeTransferFrom(loanToken, V, address(this), repaidUnits)` — loan tokens pulled from `V`
5. Net result: attacker gains `seizedAssets` of collateral at zero personal cost; `V` loses `repaidUnits` of loan token.

**Why existing checks fail:**

- The `liquidatorGate` check (lines 597–600) only gates whether `msg.sender` can liquidate; it does not constrain `callback`.
- The `onLiquidate` return-value check (lines 699–713) only verifies the return value, not that `callback` consented to being the payer.
- There is no `isAuthorized` or equivalent check on the `callback` parameter relative to `msg.sender`.
- `V` cannot distinguish a legitimate call from an attacker-initiated one: in both cases, Midnight is the `msg.sender` of `onLiquidate`. The `receiver` parameter is passed to `V`, but the protocol does not require `V` to check it, and many integrations will not.

## Impact Explanation
Direct theft of user funds. Any contract that (a) implements `ILiquidateCallback` returning `CALLBACK_SUCCESS` and (b) holds a Midnight approval for the loan token loses `repaidUnits` of loan token per exploit call. The attacker simultaneously receives seized collateral at no personal cost. This is an unauthorized movement of assets caused by a missing authorization check in the protocol itself, not in an external dependency.

## Likelihood Explanation
Preconditions: (1) a liquidatable borrower exists — a normal market condition; (2) a victim contract implements `onLiquidate` returning `CALLBACK_SUCCESS`; (3) that contract has approved Midnight for loan token. Conditions (2) and (3) are both satisfied by any flash-liquidation bot or protocol integration that uses Midnight's callback mechanism for its own liquidations and pre-approves Midnight. These are the primary intended users of the callback feature. The attack is repeatable as long as the victim's approval and the borrower's liquidatability hold. No privileged access is required.

## Recommendation
Enforce that `callback` is the caller or has explicitly authorized the caller. The minimal fix is to require `callback == msg.sender` when `callback != address(0)`:

```solidity
require(callback == address(0) || callback == msg.sender, UnauthorizedCallback());
address payer = callback != address(0) ? callback : msg.sender;
```

Alternatively, have `onLiquidate` return the payer address explicitly, so the callback contract controls who pays, rather than having the protocol assume the callback address is the payer.

## Proof of Concept
**Minimal Foundry test outline:**

```solidity
// VictimCallback: implements onLiquidate, returns CALLBACK_SUCCESS, pre-approves Midnight
contract VictimCallback is ILiquidateCallback {
    function onLiquidate(...) external returns (bytes4) {
        return CALLBACK_SUCCESS; // no origin check
    }
}

function test_callbackPayerTheft() public {
    // Setup: fund VictimCallback with loanToken, approve Midnight
    // Create a liquidatable borrower position
    // Attacker calls liquidate(market, idx, seizedAssets, 0, borrower, false,
    //                          attacker, address(victimCallback), "")
    // Assert: attacker received seizedAssets of collateral
    // Assert: victimCallback lost repaidUnits of loanToken
    // Assert: attacker spent 0 loanToken
}
```

The test passes with the current code and demonstrates direct theft with no privileged access required.