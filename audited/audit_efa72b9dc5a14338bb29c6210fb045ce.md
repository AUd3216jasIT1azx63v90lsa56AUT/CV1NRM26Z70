Audit Report

## Title
Callback Address Used as Payer Without Caller Authorization, Enabling Theft of Loan Tokens from Contracts Implementing `onLiquidate` - (File: src/Midnight.sol)

## Summary
In `liquidate()`, `payer` is unconditionally set to `callback` when `callback != address(0)`, with no requirement that `callback` authorized `msg.sender` to use it as payer. The sole gate between this assignment and the token pull is the `onLiquidate` return-value check, which verifies only `CALLBACK_SUCCESS`, not caller identity. Any contract that implements `onLiquidate`, returns `CALLBACK_SUCCESS` without verifying `_caller == address(this)`, and holds a Midnight approval for the loan token can be drained by an unprivileged attacker who passes that contract's address as `callback` while directing collateral to themselves via `receiver`.

## Finding Description
**Root cause — `src/Midnight.sol` lines 679 and 717:** [1](#0-0) 

```solidity
address payer = callback != address(0) ? callback : msg.sender;
``` [2](#0-1) 

```solidity
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
```

The only gate between the `payer = callback` assignment and the token pull is the return-value check at lines 698–714: [3](#0-2) 

Returning `CALLBACK_SUCCESS` is treated as implicit consent to pay `repaidUnits`. There is no check that `callback == msg.sender`, that `callback` authorized `msg.sender` to use it as payer, or that `callback` initiated the liquidation.

**Contrast with `repay()`**, which has an explicit authorization guard: [4](#0-3) 

```solidity
require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

`liquidate()` has no analogous constraint on `callback`.

**Exploit flow:**

1. Victim contract `V` legitimately integrates with Midnight: it implements `onLiquidate`, verifies `msg.sender == midnight` (the protocol), acquires loan tokens, approves Midnight, and returns `CALLBACK_SUCCESS` — but does not verify `_caller == address(this)`.
2. Attacker identifies a liquidatable borrower `B`.
3. Attacker calls:
   ```solidity
   midnight.liquidate(market, idx, seizedAssets, 0, B, false, attacker, V, "");
   ```
   with `callback = V`, `receiver = attacker`.
4. Execution:
   - Line 679: `payer = V`
   - Line 696: `safeTransfer(collateralToken, attacker, seizedAssets)` — attacker receives collateral.
   - Lines 698–714: `V.onLiquidate(attacker, ...)` is called; `V` verifies `msg.sender == midnight` ✓, does not check `_caller`, proceeds with its normal logic (acquires loan tokens, approves Midnight), returns `CALLBACK_SUCCESS`.
   - Line 717: `safeTransferFrom(loanToken, V, address(this), repaidUnits)` — loan tokens pulled from `V`.
5. Net result: attacker gains `seizedAssets` of collateral at zero personal cost; `V` loses `repaidUnits` of loan token.

**Why existing checks do not stop it:**

- The `liquidatorGate` check (lines 597–600) only gates whether `msg.sender` can liquidate; it does not constrain `callback`. [5](#0-4) 

- The `onLiquidate` return-value check verifies only the return value, not that `callback` consented to being the payer for this specific caller.
- There is no `isAuthorized` or equivalent check on the `callback` parameter in `liquidate()`.

**The test contract itself does not check `_caller`**, confirming this is a non-obvious security requirement: [6](#0-5) 

The test's `onLiquidate` only checks `_id == IdLib.toId(_market, block.chainid, msg.sender)` (verifying Midnight is the caller), but never checks `_caller`.

The Certora spec `OnlyExplicitPayerCanLoseTokens.spec` models `CALLBACK_SUCCESS` as sufficient authorization for the token pull, confirming this is the protocol's intended design — but the design does not enforce that the callback verified who initiated the liquidation: [7](#0-6) 

## Impact Explanation
Direct theft of loan tokens from any contract that (a) implements `onLiquidate` returning `CALLBACK_SUCCESS` without checking `_caller == address(this)` and (b) holds a Midnight approval for the loan token. The attacker simultaneously receives seized collateral at no personal cost. The loss is concrete, repeatable, and caused entirely by the protocol's missing authorization check — not by an external dependency alone.

## Likelihood Explanation
Preconditions: (1) a liquidatable borrower exists (normal market condition); (2) a victim contract implements `onLiquidate` returning `CALLBACK_SUCCESS` without checking `_caller`; (3) that contract has approved Midnight for the loan token. Conditions (2) and (3) are both satisfied by flash-liquidation bots and protocol integrations that use Midnight's callback mechanism for their own liquidations and pre-approve Midnight — the primary intended users of the callback feature. The `_caller` check is non-obvious: the protocol does not document it as a required security invariant, and the protocol's own test contract (`test/LiquidationTest.sol` lines 985–1009) does not perform it. The attack is executable by any unprivileged user with no special access.

## Recommendation
Add an authorization check on `callback` in `liquidate()`, analogous to the check in `repay()`:

```solidity
if (callback != address(0)) {
    require(callback == msg.sender || isAuthorized[callback][msg.sender], Unauthorized());
}
```

This ensures that only a caller who is the callback itself, or who has been explicitly authorized by the callback, can designate that callback as payer. Alternatively, document `_caller` verification as a mandatory security invariant for all `onLiquidate` implementations and update the test contract accordingly — but the on-chain enforcement approach is strongly preferred.

## Proof of Concept
**Minimal Foundry test:**

1. Deploy a `VictimLiquidator` contract that:
   - Implements `onLiquidate`, verifies `msg.sender == address(midnight)`, acquires `repaidUnits` of loan token (e.g., from its own balance), approves Midnight, and returns `CALLBACK_SUCCESS` — without checking `_caller`.
   - Pre-approves Midnight for the loan token.
2. Set up a market with a liquidatable borrower `B`.
3. As `attacker` (a separate EOA), call:
   ```solidity
   midnight.liquidate(market, 0, seizedAssets, 0, B, true, attacker, address(victimLiquidator), "");
   ```
4. Assert:
   - `attacker` received `seizedAssets` of collateral token.
   - `victimLiquidator` lost `repaidUnits` of loan token.
   - `attacker` spent zero loan tokens.

The test contract at `test/LiquidationTest.sol` lines 985–1009 already provides a near-complete victim implementation; adding a loan token balance and approval to `address(this)` before calling `liquidate` with `receiver = attacker` and `callback = address(this)` from a separate attacker address would reproduce the exploit directly.

### Citations

**File:** src/Midnight.sol (L505-505)
```text
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L597-600)
```text
        require(
            market.liquidatorGate == address(0) || ILiquidatorGate(market.liquidatorGate).canLiquidate(msg.sender),
            LiquidatorGatedFromLiquidating()
        );
```

**File:** src/Midnight.sol (L679-679)
```text
        address payer = callback != address(0) ? callback : msg.sender;
```

**File:** src/Midnight.sol (L698-714)
```text
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
```

**File:** src/Midnight.sol (L717-717)
```text
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
```

**File:** test/LiquidationTest.sol (L985-1009)
```text
    function onLiquidate(
        address _caller,
        bytes32 _id,
        Market memory _market,
        uint256 _collateralIndex,
        uint256 _seizedAssets,
        uint256 _repaidUnits,
        address _borrower,
        address _receiver,
        bytes memory data,
        uint256 badDebt
    ) public returns (bytes32) {
        require(_id == IdLib.toId(_market, block.chainid, msg.sender), "wrong id");
        recordedCaller = _caller;
        recordedId = _id;
        recordedMarket = _market;
        recordedBorrower = _borrower;
        recordedReceiver = _receiver;
        recordedCollateralIndex = _collateralIndex;
        recordedSeizedAssets = _seizedAssets;
        recordedRepaidUnits = _repaidUnits;
        recordedBadDebt = badDebt;
        recordedData = data;
        return CALLBACK_SUCCESS;
    }
```

**File:** certora/specs/OnlyExplicitPayerCanLoseTokens.spec (L127-130)
```text
    buyCallbackAllowed = false;
    liquidateCallbackAllowed = f.selector == sig:liquidate(Midnight.Market, uint256, uint256, uint256, address, bool, address, address, bytes).selector;
    repayCallbackAllowed = f.selector == sig:repay(Midnight.Market, uint256, address, address, bytes).selector;
    flashLoanCallbackAllowed = f.selector == sig:flashLoan(address[], uint256[], address, bytes).selector;
```
