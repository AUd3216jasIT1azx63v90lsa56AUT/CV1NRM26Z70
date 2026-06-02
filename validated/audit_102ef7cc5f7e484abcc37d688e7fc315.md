Audit Report

## Title
Fee-on-Transfer Collateral Token Inflates `position.collateral` Beyond Actual Protocol Balance - (File: `src/Midnight.sol`)

## Summary

`supplyCollateral` credits `position.collateral[collateralIndex]` with the full `assets` parameter before invoking `SafeTransferLib.safeTransferFrom`, which only verifies call success and a `true` return value — not the amount actually received. Because `touchMarket` is permissionless and imposes no restriction on collateral token transfer semantics, any unprivileged actor can create a market whose collateral token silently deducts a fee on `transferFrom`, permanently breaking the protocol's solvency invariant that contract balances must cover all collateral claims.

## Finding Description

**Root cause — `supplyCollateral` (`src/Midnight.sol` lines 524–546):**

`position.collateral[collateralIndex]` is incremented by the full `assets` value at line 533 before the transfer executes at line 545:

```solidity
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets); // line 533
...
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets); // line 545
```

**`SafeTransferLib.safeTransferFrom` (`src/libraries/SafeTransferLib.sol` lines 24–34)** only checks that the low-level call did not revert and that the return value (if any) decoded to `true`. It performs no balance-before/balance-after check and cannot detect a fee-on-transfer token that returns `true` while delivering fewer tokens than requested.

**`touchMarket` (`src/Midnight.sol` lines 755–791)** validates only maturity, collateral count, sorted addresses, allowed LLTV tiers, and valid `maxLif`. It performs no validation of the collateral token's transfer semantics — any deployed contract address is accepted.

**Exploit flow:**
1. Attacker deploys `FeeToken` — a standard ERC20 that deducts 1% from the recipient on every `transferFrom`, returns `true`.
2. Attacker calls `touchMarket` with `FeeToken` as collateral, a valid LLTV tier, and valid `maxLif`. Market is created permissionlessly.
3. Attacker calls `supplyCollateral(market, 0, 1000e18, attacker)`:
   - Line 533 sets `position[id][attacker].collateral[0] = 1000e18`.
   - Line 545 calls `safeTransferFrom(FeeToken, attacker, address(this), 1000e18)` — protocol receives `990e18`.
4. Protocol state: recorded collateral = `1000e18`; actual balance = `990e18`. Divergence = `10e18`.
5. Attacker borrows against the inflated `1000e18` collateral value, creating structurally undercollateralized debt.
6. On `withdrawCollateral` or `liquidate`, `safeTransfer(FeeToken, receiver, 1000e18)` reverts because the protocol only holds `990e18`.

**Why existing checks do not stop it:**
- `safeTransferFrom` checks only `success && (returndata == true)` — a fee-on-transfer token satisfies both.
- There is no `balanceBefore`/`balanceAfter` guard in `supplyCollateral`.
- `touchMarket` has no token allowlist or transfer-semantics check.
- The health check in `withdrawCollateral` (`isHealthy`) uses `position.collateral` (the inflated value), not the real balance.
- The formal verification in `certora/specs/Solvency.spec` line 31 explicitly assumes "ERC20 tokens transfer correctly: no fee taking from sender or receiver" — this is a prover modeling assumption, not an on-chain enforcement.

## Impact Explanation

The protocol's core solvency invariant — "contract token balances must cover withdrawable assets, collateral claims, credit redemptions, and accrued fees" — is broken: `IERC20(collateralToken).balanceOf(address(midnight)) < Σ position[id][user].collateral[collateralIndex]`. The core invariant "ERC20 transfer deltas must match accounting deltas" is also violated.

Concrete downstream consequences:
- **Bad debt creation**: Borrowers borrow against phantom collateral, creating structurally undercollateralized debt that cannot be recovered.
- **Liquidation freeze**: `liquidate` reverts when attempting to seize the full recorded collateral, leaving unhealthy/overdue positions permanently unliquidatable.
- **Withdrawal freeze**: `withdrawCollateral` reverts for the last withdrawer(s) because `safeTransfer` cannot send more than the actual balance.
- **Lender loss**: Bad debt from phantom-collateral borrowing is realized against lender credit proportionally, causing direct loss of lender funds.

## Likelihood Explanation

- **Preconditions**: Attacker deploys a fee-on-transfer ERC20 (trivial, no privilege required) and creates a market with it via the permissionless `touchMarket`.
- **Feasibility**: Fully on-chain, no oracle manipulation, no admin key, no user mistake required. The attacker is a "market creator" — an explicitly modeled attacker role per `live_context.json`.
- **Repeatability**: Every `supplyCollateral` call with a fee-on-transfer token widens the gap. Multiple users in the same market compound the insolvency.
- **Victim scope**: Lenders who fill offers in the affected market are harmed by bad debt realization even if they never interact with the fee token directly.
- **Not excluded**: `SECURITY.md` contains no exclusion for fee-on-transfer tokens. `live_context.json` explicitly lists "token charges fee" as a recommended fuzz axis and states fee-on-transfer tokens "should be tested if not explicitly excluded."

## Recommendation

Add a balance-before/balance-after check in `supplyCollateral` to credit only the actual received amount:

```solidity
uint256 balanceBefore = IERC20(collateralToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
uint256 received = IERC20(collateralToken).balanceOf(address(this)) - balanceBefore;
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + received);
```

Alternatively, explicitly document and enforce that fee-on-transfer tokens are not supported as collateral (e.g., via a token allowlist in `touchMarket` or a documented invariant enforced by a deployment-time check), and add this exclusion to `SECURITY.md`.

## Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import "../src/Midnight.sol";

contract FeeToken {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    
    function mint(address to, uint256 amount) external { balanceOf[to] += amount; }
    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount; return true;
    }
    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        uint256 fee = amount / 100; // 1% fee
        balanceOf[to] += amount - fee; // recipient gets 99%
        return true;
    }
    function transfer(address to, uint256 amount) external returns (bool) {
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }
    function code() external view returns (bytes memory) { return address(this).code; }
}

contract FeeOnTransferPoC is Test {
    Midnight midnight;
    FeeToken feeToken;
    address attacker = address(0xBEEF);

    function setUp() public {
        midnight = new Midnight(...); // deploy with valid params
        feeToken = new FeeToken();
        feeToken.mint(attacker, 1000e18);
    }

    function testFeeOnTransferInflatesCollateral() public {
        // Build market with feeToken as collateral
        Market memory market = ...; // valid market params with feeToken
        
        vm.startPrank(attacker);
        feeToken.approve(address(midnight), 1000e18);
        midnight.supplyCollateral(market, 0, 1000e18, attacker);
        vm.stopPrank();

        // Protocol recorded 1000e18 but only received 990e18
        uint256 recorded = midnight.position(marketId, attacker).collateral[0];
        uint256 actual = feeToken.balanceOf(address(midnight));
        
        assertEq(recorded, 1000e18);      // inflated
        assertEq(actual, 990e18);         // real balance
        assertGt(recorded, actual);       // solvency invariant broken
    }
}
```