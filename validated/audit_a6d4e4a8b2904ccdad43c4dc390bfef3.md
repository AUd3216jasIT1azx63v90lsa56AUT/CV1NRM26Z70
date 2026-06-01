Audit Report

## Title
Flash Loan Callback Enables Zero-Capital Spot-Oracle Manipulation to Drain Authorized Borrower Collateral - (File: src/Midnight.sol)

## Summary
`flashLoan` (lines 737–752) carries no reentrancy guard and performs no health assertion before or after its callback. An attacker holding a borrower's authorization can call `withdrawCollateral` inside `onFlashLoan`, where `isHealthy` reads a transiently inflated spot-oracle price caused by the flash-loaned tokens. After the flash loan is repaid and the oracle price reverts, the borrower is left undercollateralized with bad debt that is socialized to lenders.

## Finding Description

**Code path.**

`flashLoan` transfers tokens out, fires the callback, then pulls tokens back with no lock and no health assertion at any point:

```solidity
// src/Midnight.sol:742-751
for (uint256 i = 0; i < tokens.length; i++) {
    SafeTransferLib.safeTransfer(tokens[i], callback, assets[i]);   // balance drops
}
require(
    IFlashLoanCallback(callback).onFlashLoan(msg.sender, tokens, assets, data) == CALLBACK_SUCCESS,
    WrongFlashLoanCallbackReturnValue()
);                                                                   // arbitrary re-entry
for (uint256 i = 0; i < tokens.length; i++) {
    SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i]); // balance restored
}
```

Inside `onFlashLoan`, the attacker calls `withdrawCollateral`. The only health gate is:

```solidity
// src/Midnight.sol:568
require(isHealthy(market, id, onBehalf), UnhealthyBorrower());
```

`isHealthy` makes a live external call to the oracle:

```solidity
// src/Midnight.sol:953
uint256 price = IOracle(collateralParam.oracle).price();
```

`IOracle` is a one-function interface with no manipulation-resistance requirement. If the oracle is a spot-price oracle (e.g., Uniswap V2/V3 instantaneous reserve ratio), its return value is a function of the pool's token balance at call time.

**Exploit flow.**

Preconditions: `isAuthorized[borrower][attacker] == true`; market oracle is a spot-price oracle; borrower is at or near the LLTV boundary.

1. Attacker calls `flashLoan([loanToken], [largeAmount], attackerContract, data)`.
2. Midnight sends `largeAmount` loan tokens to `attackerContract`.
3. `onFlashLoan` executes:
   - Attacker swaps flash-loaned loan tokens for collateral tokens in the AMM → collateral spot price inflates.
   - Attacker calls `midnight.withdrawCollateral(market, collateralIndex, assets, borrower, attacker)`:
     - Authorization check (line 556) passes — attacker is authorized.
     - Collateral storage is decremented (line 562).
     - `isHealthy` (line 568) calls oracle → reads inflated price → returns `true`.
     - Collateral tokens transferred to attacker (line 572).
   - Attacker swaps withdrawn collateral back to loan tokens in the AMM → oracle price deflates.
   - Attacker approves Midnight for flash loan repayment.
4. Midnight pulls loan tokens back (line 750) — repayment succeeds.
5. Post-transaction: borrower's collateral is reduced, debt is unchanged, oracle price is back to normal → `isHealthy` now returns `false` → borrower is immediately liquidatable with bad debt.

**Why existing checks fail.**

- **Authorization check (line 556):** passes by precondition.
- **`isHealthy` check (line 568):** reads a live, manipulable oracle price; no snapshot, no TWAP enforcement, no check that price is the same before and after the callback.
- **TOKEN SAFETY REQUIREMENTS (lines 133–140):** line 138 prohibits token re-entry on `transfer`/`transferFrom` only; it says nothing about re-entry through the flash loan callback, which is an explicit, designed callback path.
- **Certora `Healthiness.spec` (line 14–16):** explicitly states `"Assumption: price does not change during rules"` and models `price()` via a `persistent ghost summaryPrice(calledContract)`. This is a formal-verification modeling assumption that does not hold for spot-price oracles and is not enforced on-chain.
- **`OnlyAuthorizedCanChange.spec` (lines 30–31):** explicitly states `"Assume no reentrancy: callbacks and tokens do not re-enter Midnight"` — again a verification assumption, not an on-chain guard.
- **No reentrancy guard:** confirmed absent on both `flashLoan` and `withdrawCollateral`.

## Impact Explanation

An authorized attacker can withdraw collateral from a borrower's position while the health check is satisfied only by a transiently inflated oracle price. After the flash loan unwinds, the borrower's `debt > maxDebt`, making the position immediately liquidatable. If the withdrawn collateral exceeds the bad-debt threshold, lenders absorb a loss through the loss-factor socialization mechanism. The attacker profits by the withdrawn collateral minus AMM slippage, with zero net capital required. This constitutes direct theft of user assets and forced bad debt on lenders — both in-scope impact classes. SECURITY.md line 26 explicitly states: *"Note: This does not exclude oracle manipulation/flash-loan attacks."*

## Likelihood Explanation

Preconditions are realistic and repeatable:
- Spot-price oracles (Uniswap V2/V3 instantaneous price) are a common oracle choice; the protocol imposes no oracle requirements anywhere.
- Borrower authorization is a standard DeFi pattern (e.g., a borrower authorizes a position-management contract that is later compromised or is itself malicious). The protocol documentation (lines 101–110) explicitly describes authorization as a user-level feature accessible to any authorized smart contract.
- The flash loan is permissionless and requires no upfront capital.
- The attack is atomic and repeatable in any block.

## Recommendation

1. **Add a reentrancy guard to `flashLoan`** (and any other function that fires external callbacks) using a transient-storage lock, preventing re-entry into state-mutating functions like `withdrawCollateral` during the callback.
2. **Snapshot the health state before the flash loan callback and re-assert it after**, or alternatively prohibit calls to `withdrawCollateral` (and other position-mutating functions) while a flash loan callback is in progress.
3. **Enforce oracle manipulation-resistance** in market creation: require oracles to implement a TWAP or similar manipulation-resistant price feed, or document and enforce this as a market parameter requirement.
4. **Update the Certora `Healthiness.spec`** to model the `PER_CALLEE_CONSTANT` price assumption as a verified invariant rather than an unverified axiom, or add a rule that explicitly covers the flash-loan re-entry scenario.

## Proof of Concept

**Minimal fork test outline (Foundry):**

```solidity
// 1. Deploy a market with a Uniswap V2 spot-price oracle for collateralToken/loanToken.
// 2. Borrower supplies collateral and takes debt, positioning at 99% of LLTV.
// 3. Borrower calls midnight.setIsAuthorized(attackerContract, true, borrower).
// 4. AttackerContract.attack():
//    a. calls midnight.flashLoan([loanToken], [largeAmount], address(this), "")
//    b. onFlashLoan():
//       - swap largeAmount loanToken → collateralToken via Uniswap V2 (inflates collateral price)
//       - call midnight.withdrawCollateral(market, 0, maxWithdrawable, borrower, address(this))
//         → isHealthy reads inflated price → passes
//       - swap withdrawn collateralToken → loanToken via Uniswap V2 (deflates price)
//       - approve midnight for largeAmount loanToken repayment
//    c. flash loan repaid successfully
// 5. Assert: midnight.isHealthy(market, id, borrower) == false (borrower now undercollateralized)
// 6. Assert: attackerContract.balance(collateralToken) > 0 (collateral stolen)
// 7. Assert: liquidating borrower realizes bad debt (lenders lose funds)
```

The test is deterministic: the price manipulation magnitude needed is `(currentDebt / currentCollateral) / LLTV - currentPrice`, achievable with a flash loan sized proportionally to the AMM pool depth.