### Title
Arbitrary `callback` address can be forced to pay for liquidations — (`src/Midnight.sol`)

### Summary

The `liquidate` function in `Midnight.sol` sets `payer = callback` when `callback != address(0)`, but never validates that `callback` is controlled by or authorized by `msg.sender`. Any caller can pass an arbitrary contract as `callback`, forcing it to pay `repaidUnits` of loan tokens while the caller receives the seized collateral. The same structural flaw exists in `repay` and `take`.

---

### Finding Description

**Vulnerability type:** Authorization bypass (analog to M-21: liquidation on behalf of another account)

In `liquidate`, the payer for the loan token repayment is determined as: [1](#0-0) 

```solidity
address payer = callback != address(0) ? callback : msg.sender;
```

The only authorization check in `liquidate` is against `msg.sender` via the `liquidatorGate`: [2](#0-1) 

There is no check that `callback` is `msg.sender`, is authorized by `msg.sender`, or is in any way controlled by the caller. The execution flow is:

1. Seized collateral is transferred to `receiver` (attacker-controlled): [3](#0-2) 

2. `ILiquidateCallback(callback).onLiquidate(...)` is called on the victim contract: [4](#0-3) 

3. Loan tokens are pulled from `payer` (= `callback`, the victim): [5](#0-4) 

**Attack path:**
- Attacker calls `liquidate(market, collateralIndex, seizedAssets, 0, borrower, false, attacker_address, victim_callback, "")`
- `repaidUnits` is computed from `seizedAssets`
- Seized collateral goes to `attacker_address`
- `victim_callback.onLiquidate(...)` is called; if it returns `CALLBACK_SUCCESS`, execution continues
- `safeTransferFrom(loanToken, victim_callback, Midnight, repaidUnits)` drains loan tokens from the victim

**Same pattern in `repay`:** [6](#0-5) 

The `onBehalf` parameter is authorization-checked, but `callback` (the payer) is not. An attacker sets `onBehalf = msg.sender` and `callback = victim_callback` to repay their own debt using the victim's funds.

**Same pattern in `take` (taker-as-buyer path):** [7](#0-6) 

When `offer.buy == false`, `buyerCallback = takerCallback` (attacker-controlled). Setting `takerCallback = victim_callback` forces the victim to pay `buyerAssets` while the attacker receives credit units.

---

### Impact Explanation

Direct loss of loan tokens for any contract that:
1. Implements `ILiquidateCallback` / `IRepayCallback` / `IBuyCallback` and returns `CALLBACK_SUCCESS`
2. Has a standing ERC-20 approval to `Midnight`

The attacker receives seized collateral (real value) at zero cost; the victim contract loses loan tokens equal to `repaidUnits`. This is a stronger impact than M-21, where the "loss" was receiving unwanted vault shares rather than a direct token drain.

---

### Likelihood Explanation

Realistic preconditions:
- Liquidation helper contracts, flash-loan wrappers, and protocol integrations routinely implement callback interfaces and maintain token approvals to the core protocol
- A victim callback that does not validate `caller` (the liquidator address passed into `onLiquidate`) is exploitable; many implementations only check `msg.sender == Midnight`, which passes

The attacker needs no privileged access — only knowledge of a deployed callback contract with an approval.

---

### Recommendation

Add a check in `liquidate`, `repay`, and `take` that the `callback` address is either absent, equal to `msg.sender`, or explicitly authorized by `msg.sender`:

```solidity
// In liquidate / repay / take, before using callback as payer:
require(
    callback == address(0) || callback == msg.sender || isAuthorized[msg.sender][callback],
    Unauthorized()
);
```

This mirrors the existing `isAuthorized` pattern used throughout the contract for `onBehalf` checks. [8](#0-7) [9](#0-8) 

---

### Proof of Concept

```solidity
// VictimLiquidator: a legitimate liquidation helper that has approved Midnight
contract VictimLiquidator is ILiquidateCallback {
    function onLiquidate(...) external returns (bytes32) {
        // Only checks msg.sender == Midnight, not caller (the liquidator)
        return CALLBACK_SUCCESS;
    }
}

// Setup: VictimLiquidator has approved Midnight for loanToken

// Attack:
midnight.liquidate(
    market,
    collateralIndex,
    seizedAssets,   // > 0
    0,              // repaidUnits computed from seizedAssets
    borrower,
    false,
    attacker,                  // receiver: attacker gets collateral
    address(victimLiquidator), // callback: victim pays loan tokens
    ""
);
// Result: attacker receives seizedAssets of collateral
//         victimLiquidator loses repaidUnits of loanToken
```

### Citations

**File:** src/Midnight.sol (L422-422)
```text
        address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
```

**File:** src/Midnight.sol (L482-482)
```text
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L505-505)
```text
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L511-511)
```text
        address payer = callback != address(0) ? callback : msg.sender;
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

**File:** src/Midnight.sol (L696-696)
```text
        SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
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
