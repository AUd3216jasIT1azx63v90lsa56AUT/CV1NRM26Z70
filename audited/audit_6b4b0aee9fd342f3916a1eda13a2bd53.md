Audit Report

## Title
Flash Loan Callback Enables Zero-Capital Spot-Oracle Manipulation to Drain Authorized Borrower Collateral - (File: src/Midnight.sol)

## Summary
`flashLoan` (lines 737–752) has no reentrancy guard and performs no health assertion before or after its callback. An attacker holding a borrower's authorization can call `withdrawCollateral` inside `onFlashLoan`, where `isHealthy` reads a transiently inflated spot-oracle price caused by the flash-loaned tokens. After the flash loan is repaid and the oracle price reverts, the borrower is left undercollateralized with bad debt socialized to lenders.

## Finding Description

**Code path.**

`flashLoan` transfers tokens out, fires the callback, then pulls tokens back with no lock and no health assertion at any point: [1](#0-0) 

Inside `onFlashLoan`, the attacker calls `withdrawCollateral`. The only health gate is: [2](#0-1) 

`isHealthy` makes a live external call to the oracle: [3](#0-2) 

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
- **TOKEN SAFETY REQUIREMENTS (lines 133–140):** line 138 prohibits token re-entry on `transfer`/`transferFrom` only; it says nothing about re-entry through the flash loan callback, which is an explicit, designed callback path. [4](#0-3) 

- **Certora `Healthiness.spec` (line 14–16):** explicitly states `"Assumption: price does not change during rules"` and models `price()` via a `persistent ghost summaryPrice(calledContract)`. This is a formal-verification modeling assumption that does not hold for spot-price oracles and is not enforced on-chain. [5](#0-4) 

- **`OnlyAuthorizedCanChange.spec` (lines 30–31):** explicitly states `"Assume no reentrancy: callbacks and tokens do not re-enter Midnight"` — again a verification assumption, not an on-chain guard. [6](#0-5) 

- **No reentrancy guard:** confirmed absent on both `flashLoan` and `withdrawCollateral` via code inspection.

## Impact Explanation

An authorized attacker can withdraw collateral from a borrower's position while the health check is satisfied only by a transiently inflated oracle price. After the flash loan unwinds, the borrower's `debt > maxDebt`, making the position immediately liquidatable. If the withdrawn collateral exceeds the bad-debt threshold, lenders absorb a loss through the loss-factor socialization mechanism. The attacker profits by the withdrawn collateral minus AMM slippage, with zero net capital required. This constitutes direct theft of user assets and forced bad debt on lenders — both in-scope impact classes. SECURITY.md line 26 explicitly states: *"Note: This does not exclude oracle manipulation/flash-loan attacks."* [7](#0-6) 

## Likelihood Explanation

Preconditions are realistic and repeatable:
- Spot-price oracles (Uniswap V2/V3 instantaneous price) are a common oracle choice; the protocol imposes no oracle requirements anywhere.
- Borrower authorization is a standard DeFi pattern (e.g., a borrower authorizes a position-management contract that is later compromised or is itself malicious). The protocol documentation (lines 101–110) explicitly describes authorization as a user-level feature accessible to any authorized smart contract. [8](#0-7) 

- The flash loan is permissionless and requires no upfront capital.
- The attack is atomic and repeatable in any block.

## Recommendation

1. **Add a reentrancy guard** to `flashLoan` (and optionally `withdrawCollateral`) using a transient storage lock or OpenZeppelin's `ReentrancyGuard`, preventing any re-entry into Midnight state-changing functions during the flash loan callback.
2. **Alternatively or additionally**, enforce that `withdrawCollateral` cannot be called while a flash loan is in progress by tracking a `_flashLoanActive` flag set before the callback and cleared after.
3. **Require TWAP or manipulation-resistant oracles** at market creation time, or add a price-deviation check in `isHealthy` that compares the current price against a time-weighted reference.
4. **Add a post-callback health assertion** in `flashLoan` for any borrower whose collateral was modified during the callback, though this is complex to implement generically.

## Proof of Concept

**Minimal fork test plan (Foundry):**

```solidity
// 1. Deploy Midnight with a Uniswap V2 pair as the collateral oracle (spot price).
// 2. Create a borrower position at exactly the LLTV boundary.
// 3. Authorize attackerContract for the borrower.
// 4. attackerContract.attack():
//    a. Call midnight.flashLoan([loanToken], [largeAmount], address(this), "")
//    b. In onFlashLoan:
//       i.  Swap largeAmount loanToken → collateralToken via Uniswap V2 (inflates collateral price)
//       ii. Call midnight.withdrawCollateral(market, 0, fullCollateral, borrower, address(this))
//           → isHealthy reads inflated price → passes
//       iii.Swap withdrawn collateralToken → loanToken via Uniswap V2 (restores price)
//       iv. Approve midnight for largeAmount loanToken repayment
//       v.  Return CALLBACK_SUCCESS
// 5. Assert: borrower.collateral == 0, borrower.debt > 0, isHealthy(borrower) == false
// 6. Assert: attacker holds collateral tokens with net profit > 0
```

### Citations

**File:** src/Midnight.sol (L101-110)
```text
/// AUTHORIZATIONS
/// @dev All functions that change the position, consumed and authorization are accessible to the user and to
/// any account that has been authorized. Thus, to scope authorizations one should authorize a smart-contract with
/// scoped behavior.
/// @dev When authorizing a smart-contract, one should consider:
/// - The targets/functions that the account can call. At least Midnight's functions should be considered, but other
/// contracts might re-use Midnight's authorization mapping too (e.g ratifiers and authorizers). In particular,
/// authorized accounts can authorize other accounts on behalf of the user.
/// - Under which conditions the account can return CALLBACK_SUCCESS when its isRatified function is called.
/// @dev updatePosition and liquidate (for liquidatable users) also impact the position and are permissionless.
```

**File:** src/Midnight.sol (L133-140)
```text
/// TOKEN SAFETY REQUIREMENTS
/// @dev List of assumptions on tokens that guarantee that Midnight behaves as expected:
/// - It should be ERC-20 compliant, except that it can omit return values on transfer and transferFrom. In particular,
/// it should not revert because a transfer is no-op.
/// - Midnight's balance of the token should only decrease on transfer and transferFrom.
/// - It should not re-enter Midnight on transfer nor transferFrom.
/// - Midnight must send/receive exactly the requested amount on transfers.
/// @dev See LIVENESS for liveness guarantees.
```

**File:** src/Midnight.sol (L568-568)
```text
        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());
```

**File:** src/Midnight.sol (L742-751)
```text
        for (uint256 i = 0; i < tokens.length; i++) {
            SafeTransferLib.safeTransfer(tokens[i], callback, assets[i]);
        }
        require(
            IFlashLoanCallback(callback).onFlashLoan(msg.sender, tokens, assets, data) == CALLBACK_SUCCESS,
            WrongFlashLoanCallbackReturnValue()
        );
        for (uint256 i = 0; i < tokens.length; i++) {
            SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i]);
        }
```

**File:** src/Midnight.sol (L953-953)
```text
                uint256 price = IOracle(collateralParam.oracle).price();
```

**File:** certora/specs/Healthiness.spec (L14-16)
```text
    // Assumption: price does not change during rules.
    // Under this assumption we can prove that a healthy borrower cannot get unhealthy by any action on the contract.
    function _.price() external => summaryPrice(calledContract) expect(uint256);
```

**File:** certora/specs/OnlyAuthorizedCanChange.spec (L30-31)
```text
    // Assume no reentrancy: callbacks and tokens do not re-enter Midnight.
    // This is justified because the properties we verify are about the effect of each function's own body on the state, not the effect of the full transaction including callbacks.
```

**File:** SECURITY.md (L26-26)
```markdown
Note: This does not exclude oracle manipulation/flash-loan attacks.
```
