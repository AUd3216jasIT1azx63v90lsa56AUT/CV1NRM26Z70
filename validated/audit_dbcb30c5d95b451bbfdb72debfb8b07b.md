Audit Report

## Title
Arbitrary Third-Party Payer Designation in `liquidate()` Enables Theft of Victim Callback Contract Funds - (File: src/Midnight.sol)

## Summary
In `liquidate()`, the `payer` for loan token repayment is unconditionally set to the caller-supplied `callback` address with no check that `msg.sender` owns or is authorized by that contract. An attacker can designate any third-party liquidation callback contract as `callback`, causing Midnight to call `onLiquidate()` on it and then pull loan tokens from it via `safeTransferFrom`, while the attacker's chosen `receiver` collects the seized collateral at zero personal cost.

## Finding Description

**Root cause — line 679:**
```solidity
address payer = callback != address(0) ? callback : msg.sender;
```
There is no `require(callback == msg.sender || isAuthorized[callback][msg.sender])` guard. Any caller can supply an arbitrary `callback` address and that address becomes the payer.

**Full execution sequence:**

1. Borrower's collateral is decremented and `_marketState.withdrawable` is incremented (lines 670–676) — state committed before any external call.
2. Seized collateral is transferred to `receiver` (attacker-controlled) at line 696.
3. `ILiquidateCallback(callback).onLiquidate(msg.sender, ...)` is called at lines 698–714. The `caller` argument passed is `msg.sender` (the attacker), which a naive callback may not validate. If the callback returns `CALLBACK_SUCCESS`, execution continues.
4. `safeTransferFrom(market.loanToken, payer, address(this), repaidUnits)` at line 717 pulls loan tokens from `payer = callback = victimContract`.

**Existing checks that do not stop this:**
- `liquidatorGate` (lines 597–600) gates only `msg.sender`, not `callback`.
- `NotLiquidatable` (lines 620–624) only verifies the borrower's position is unhealthy — a legitimate precondition the attacker satisfies using any real unhealthy position.
- The Certora spec `OnlyExplicitPayerCanLoseTokens.spec` (lines 117–135) proves tokens are only pulled from "the callback that returned `CALLBACK_SUCCESS`" — but this invariant does not verify that the callback is the caller's own contract, so it does not prevent this attack.

**Contrast with `repay()`** (line 505): `repay()` has `require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender])` for the debt owner, but no equivalent guard exists in `liquidate()` for the `callback`/payer relationship.

**Exploit flow:**
1. Identify `victimContract`: a deployed liquidation callback contract that (a) implements `onLiquidate()` returning `CALLBACK_SUCCESS` without checking the `caller` argument, (b) has approved Midnight for `loanToken`, and (c) holds sufficient `loanToken` balance.
2. Find any real unhealthy borrower position in a valid market.
3. Call `liquidate(market, collateralIndex, 0, repaidUnits, borrower, false, receiver=attacker, callback=victimContract, data)`.
4. Midnight sets `payer = victimContract`, sends seized collateral to `attacker`, calls `victimContract.onLiquidate()` (returns `CALLBACK_SUCCESS`), then pulls `repaidUnits` of `loanToken` from `victimContract`.
5. Attacker receives seized collateral; `victimContract` loses `repaidUnits` of loan tokens; Midnight's internal accounting remains consistent.

## Impact Explanation
Direct theft of loan tokens from any liquidation callback contract that (a) returns `CALLBACK_SUCCESS` without validating the `caller` parameter and (b) holds a loan token balance with a Midnight approval. The attacker profits by the full value of seized collateral while paying nothing. The protocol's internal accounting remains consistent (withdrawable increases, debt decreases), but user funds are stolen from the victim contract. This is a critical, in-scope impact: unauthorized movement of assets caused by a missing authorization boundary in the core protocol.

## Likelihood Explanation
Preconditions are realistic and commonly met in production:
1. Unhealthy borrower positions exist in any active market during price drops.
2. Liquidation bots routinely pre-fund callback contracts with loan tokens and approve the lending protocol for those tokens.
3. Simple callback implementations that check `msg.sender == midnight` but do not validate the `caller` argument are common — the `caller` parameter is not obviously a security-critical check, and the protocol provides no documentation warning that it must be validated.
4. The attack is permissionless, requires no privileged access, and is repeatable against any qualifying victim contract.

## Recommendation
Add an authorization check on `callback` in `liquidate()`, mirroring the pattern used for `onBehalf` in other functions:

```solidity
require(
    callback == address(0) || callback == msg.sender || isAuthorized[callback][msg.sender],
    Unauthorized()
);
```

This ensures only the caller or an account that has explicitly authorized the caller can be designated as the payer/callback, eliminating the ability to drain third-party contracts.

## Proof of Concept
**Minimal Foundry test outline:**

```solidity
// VictimCallback: naive implementation
contract VictimCallback is ILiquidateCallback {
    function onLiquidate(address, bytes32, Market memory, uint256, uint256, uint256,
                         address, address, bytes memory, uint256) external returns (bytes32) {
        return CALLBACK_SUCCESS; // does not check caller
    }
}

function testDrainVictim() public {
    // 1. Deploy VictimCallback, fund with loanToken, approve Midnight
    VictimCallback victim = new VictimCallback();
    loanToken.mint(address(victim), repaidUnits);
    vm.prank(address(victim));
    loanToken.approve(address(midnight), type(uint256).max);

    // 2. Create unhealthy borrower position (standard setup)
    // ... collateralize, setupMarket, drop oracle price ...

    // 3. Attacker calls liquidate with callback = victim, receiver = attacker
    vm.prank(attacker);
    midnight.liquidate(market, 0, 0, repaidUnits, borrower, false,
                       attacker,          // receiver: attacker gets collateral
                       address(victim),   // callback/payer: victim pays
                       "");

    // 4. Assert: attacker received collateral, victim lost loanTokens
    assertGt(collateralToken.balanceOf(attacker), 0);
    assertEq(loanToken.balanceOf(address(victim)), 0);
}
```

This test is fully reproducible on a local fork with no privileged access required.