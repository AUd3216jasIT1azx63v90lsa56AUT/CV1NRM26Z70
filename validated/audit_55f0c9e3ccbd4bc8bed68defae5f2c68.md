Audit Report

## Title
Unprivileged liquidator can designate arbitrary third-party contract as `callback`/`payer`, draining its loanToken approval to Midnight - (File: src/Midnight.sol)

## Summary
`Midnight.liquidate` unconditionally sets `payer = callback` when `callback != address(0)` at line 679, with no requirement that `callback == msg.sender` or that `callback` has authorized `msg.sender`. An attacker can supply any contract implementing `ILiquidateCallback` that returns `CALLBACK_SUCCESS` and holds a loanToken approval to Midnight as the `callback`, causing the protocol to pull `repaidUnits` of loanToken from that victim contract while the attacker's chosen `receiver` collects the seized collateral for free.

## Finding Description
**Confirmed code path:**

`src/Midnight.sol` line 679:
```solidity
address payer = callback != address(0) ? callback : msg.sender;
```

`src/Midnight.sol` lines 696–717:
```solidity
SafeTransferLib.safeTransfer(collateralToken, receiver, seizedAssets);       // L696
if (callback != address(0)) {
    require(
        ILiquidateCallback(callback).onLiquidate(msg.sender, ...) == CALLBACK_SUCCESS,  // L700-714
        WrongLiquidateCallbackReturnValue()
    );
}
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits); // L717
```

**Root cause:** No check `require(callback == msg.sender || isAuthorized[callback][msg.sender])` exists anywhere in `liquidate`. The only gate on `callback` is that it must return `CALLBACK_SUCCESS` from `onLiquidate`. The `liquidatorGate` check at line 598 only gates who can call `liquidate`, not what `callback` is.

**Exploit flow:**
1. Precondition: `victimContract` implements `ILiquidateCallback`, its `onLiquidate` returns `CALLBACK_SUCCESS` (e.g., only checks `msg.sender == midnight`), and it holds `loanToken.approve(midnight, N)`.
2. Attacker calls: `midnight.liquidate(market, idx, 0, repaidUnits, borrower, false, attackerEOA, victimContract, data)`
3. `payer = victimContract` (line 679)
4. `seizedAssets` of collateral transferred to `attackerEOA` (line 696)
5. `victimContract.onLiquidate(attacker, ...)` called — returns `CALLBACK_SUCCESS` (lines 700–714)
6. `safeTransferFrom(loanToken, victimContract, midnight, repaidUnits)` executes (line 717) — pulls from victim
7. Net: attacker receives collateral worth `repaidUnits * LIF`, pays nothing; victim loses `repaidUnits` of loanToken

## Impact Explanation
Any contract that (a) implements `ILiquidateCallback` returning `CALLBACK_SUCCESS` and (b) holds a loanToken approval to Midnight can have its entire approved balance drained by an unprivileged attacker in a single `liquidate` call. The attacker receives seized collateral at no personal cost. This is direct theft of funds from third-party contracts and violates the core invariant that liquidation must not transfer assets from unauthorized parties.

## Likelihood Explanation
**Preconditions:**
1. An unhealthy borrower exists — routine market condition.
2. A victim contract implementing `ILiquidateCallback` with a loanToken approval to Midnight — any deployed liquidation bot or flash-liquidation helper that pre-approves the lending protocol for loanToken satisfies this. Such contracts commonly check only `msg.sender == midnight` in `onLiquidate`, not the `caller` parameter.

**Feasibility:** High. The attacker needs no special privilege, no oracle manipulation, and no governance access. The attack is repeatable as long as the victim's approval persists.

## Recommendation
Add an authorization check on `callback` before it is used as `payer`. Immediately after the `liquidatorGate` check (line 600), add:

```solidity
require(
    callback == address(0) || callback == msg.sender || isAuthorized[callback][msg.sender],
    Unauthorized()
);
```

This mirrors the existing authorization pattern used in `setConsumed` (line 724) and ensures that only the caller or a contract that has explicitly authorized the caller can be designated as the payer.

## Proof of Concept
**Minimal Foundry test plan:**

```solidity
// VictimCallback: implements ILiquidateCallback, returns CALLBACK_SUCCESS,
// only checks msg.sender == address(midnight)
contract VictimCallback is ILiquidateCallback {
    function onLiquidate(...) external returns (bytes32) {
        require(msg.sender == address(midnight));
        return CALLBACK_SUCCESS;
    }
}

function test_drainVictimCallback() public {
    // 1. Deploy VictimCallback, fund it with loanToken, approve Midnight
    VictimCallback victim = new VictimCallback();
    loanToken.mint(address(victim), repaidUnits);
    vm.prank(address(victim));
    loanToken.approve(address(midnight), type(uint256).max);

    // 2. Create unhealthy borrower position (standard setup)
    // ...

    // 3. Attacker calls liquidate with callback = victim, receiver = attacker
    vm.prank(attacker);
    midnight.liquidate(market, idx, 0, repaidUnits, borrower, false, attacker, address(victim), "");

    // 4. Assert: attacker received collateral, victim lost loanToken
    assertGt(collateralToken.balanceOf(attacker), 0);
    assertEq(loanToken.balanceOf(address(victim)), 0);
    assertEq(loanToken.balanceOf(attacker), 0); // attacker paid nothing
}
```