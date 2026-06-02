Audit Report

## Title
Flash Loan Callback Enables Zero-Capital Spot-Oracle Manipulation to Drain Authorized Borrower Collateral - (File: src/Midnight.sol)

## Summary
`flashLoan` (lines 737–752) has no reentrancy guard and performs no health assertion before or after its callback. An attacker holding a borrower's authorization can call `withdrawCollateral` inside `onFlashLoan`, where `isHealthy` reads a transiently inflated spot-oracle price caused by the flash-loaned tokens. After the flash loan is repaid and the oracle price reverts, the borrower is left undercollateralized with bad debt socialized to lenders.

## Finding Description

**Confirmed code path.**

`flashLoan` transfers tokens out, fires the callback, then pulls tokens back with no lock and no health assertion at any point: [1](#0-0) 

Inside `onFlashLoan`, the attacker calls `withdrawCollateral`. The only health gate is: [2](#0-1) 

`isHealthy` makes a live external call to the oracle with no snapshot, no TWAP enforcement, and no price-consistency check: [3](#0-2) 

`IOracle` is a single-function interface with no manipulation-resistance requirement: [4](#0-3) 

**Exploit flow.**

Preconditions: `isAuthorized[borrower][attacker] == true`; market oracle is a spot-price oracle (e.g., Uniswap V2/V3 instantaneous reserve ratio); borrower is at or near the LLTV boundary.

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
- **TOKEN SAFETY REQUIREMENTS (lines 133–140):** line 138 prohibits token re-entry on `transfer`/`transferFrom` only; it says nothing about re-entry through the flash loan callback, which is an explicit, designed callback path. [5](#0-4) 

- **Certora `Healthiness.spec` (lines 14–16):** explicitly states `"Assumption: price does not change during rules"` and models `price()` via a `persistent ghost summaryPrice(calledContract)`. This is a formal-verification modeling assumption that does not hold for spot-price oracles and is not enforced on-chain. [6](#0-5) 

- **`OnlyAuthorizedCanChange.spec` (lines 30–31):** explicitly states `"Assume no reentrancy: callbacks and tokens do not re-enter Midnight"` — again a verification assumption, not an on-chain guard. [7](#0-6) 

- **No reentrancy guard:** confirmed absent on both `flashLoan` and `withdrawCollateral` via code inspection.

## Impact Explanation

An authorized attacker can withdraw collateral from a borrower's position while the health check is satisfied only by a transiently inflated oracle price. After the flash loan unwinds, the borrower's `debt > maxDebt`, making the position immediately liquidatable. If the withdrawn collateral exceeds the bad-debt threshold, lenders absorb a loss through the loss-factor socialization mechanism. The attacker profits by the withdrawn collateral minus AMM slippage, with zero net capital required. This constitutes direct theft of user assets and forced bad debt on lenders — both in-scope impact classes. SECURITY.md line 26 explicitly states: *"Note: This does not exclude oracle manipulation/flash-loan attacks."* [8](#0-7) 

## Likelihood Explanation

Preconditions are realistic and repeatable:
- Spot-price oracles (Uniswap V2/V3 instantaneous price) are a common oracle choice; the protocol imposes no oracle requirements anywhere in `IOracle` or `Midnight.sol`.
- Borrower authorization is a standard DeFi pattern (e.g., a borrower authorizes a position-management contract that is later compromised or is itself malicious). The protocol documentation (lines 101–110) explicitly describes authorization as a user-level feature accessible to any authorized smart contract. [9](#0-8) 

- The flash loan is permissionless and requires no upfront capital.
- The attack is atomic and repeatable in any block.

## Recommendation

Apply one or more of the following mitigations:

1. **Reentrancy guard on `flashLoan` and `withdrawCollateral`:** Add a `nonReentrant` modifier (or equivalent transient-storage lock) to both functions so that `withdrawCollateral` cannot be called from within `onFlashLoan`.
2. **Oracle price snapshot:** In `withdrawCollateral`, record the oracle price before any external call and assert it has not changed after the health check, or require a TWAP oracle with a minimum observation window.
3. **Require TWAP oracles:** Document and enforce (via interface or deployment checks) that oracles must be manipulation-resistant (e.g., minimum TWAP window), rather than accepting any `IOracle` implementation.

## Proof of Concept

**Minimal fork test plan:**

```solidity
// Setup:
// - Deploy Midnight with a Uniswap V2 spot-price oracle for the collateral token.
// - Borrower supplies collateral and borrows to exactly the LLTV boundary.
// - Borrower calls setIsAuthorized(attackerContract, true, borrower).

// AttackerContract.onFlashLoan():
// 1. Swap all flash-loaned loan tokens into collateral token via Uniswap V2 → inflates collateral price.
// 2. Call midnight.withdrawCollateral(market, 0, maxWithdrawable, borrower, address(this)).
//    → isHealthy reads inflated price → passes.
// 3. Swap withdrawn collateral back to loan tokens via Uniswap V2 → price reverts.
// 4. Approve Midnight for repayment amount.
// 5. Return CALLBACK_SUCCESS.

// Assertions after tx:
// assertFalse(midnight.isHealthy(market, id, borrower));  // borrower now unhealthy
// assertGt(attackerContract.balance(collateralToken), 0); // attacker holds stolen collateral
```

The test proves the borrower is immediately liquidatable post-transaction with bad debt, while the attacker retains the withdrawn collateral minus swap slippage.

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

**File:** src/Midnight.sol (L556-572)
```text
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        address collateralToken = market.collateralParams[collateralIndex].token;

        Position storage _position = position[id][onBehalf];
        uint256 newCollateral = _position.collateral[collateralIndex] - assets;
        _position.collateral[collateralIndex] = UtilsLib.toUint128(newCollateral);

        if (newCollateral == 0 && assets > 0) {
            _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
        }

        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());

        emit EventsLib.WithdrawCollateral(msg.sender, id, collateralToken, assets, onBehalf, receiver);

        SafeTransferLib.safeTransfer(collateralToken, receiver, assets);
```

**File:** src/Midnight.sol (L737-752)
```text
    function flashLoan(address[] calldata tokens, uint256[] calldata assets, address callback, bytes calldata data)
        external
    {
        require(tokens.length == assets.length, InconsistentInput());
        emit EventsLib.FlashLoan(msg.sender, tokens, assets, callback);
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
    }
```

**File:** src/Midnight.sol (L944-960)
```text
    function isHealthy(Market memory market, bytes32 id, address borrower) public view returns (bool) {
        Position storage _position = position[id][borrower];
        uint256 debt = _position.debt;
        uint256 maxDebt;
        if (debt > 0) {
            uint128 _collateralBitmap = _position.collateralBitmap;
            while (_collateralBitmap != 0) {
                uint256 i = UtilsLib.msb(_collateralBitmap);
                CollateralParams memory collateralParam = market.collateralParams[i];
                uint256 price = IOracle(collateralParam.oracle).price();
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
                _collateralBitmap = _collateralBitmap.clearBit(i);
            }
        }
        return maxDebt >= debt;
    }
```

**File:** src/interfaces/IOracle.sol (L1-7)
```text
// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (c) 2025 Morpho Association
pragma solidity >=0.5.0;

interface IOracle {
    function price() external view returns (uint256);
}
```

**File:** certora/specs/Healthiness.spec (L14-16)
```text
    // Assumption: price does not change during rules.
    // Under this assumption we can prove that a healthy borrower cannot get unhealthy by any action on the contract.
    function _.price() external => summaryPrice(calledContract) expect(uint256);
```

**File:** certora/specs/OnlyAuthorizedCanChange.spec (L30-37)
```text
    // Assume no reentrancy: callbacks and tokens do not re-enter Midnight.
    // This is justified because the properties we verify are about the effect of each function's own body on the state, not the effect of the full transaction including callbacks.
    function _.onBuy(bytes32, Midnight.Market, uint256, uint256, uint256, address, bytes) external => NONDET;
    function _.onSell(bytes32, Midnight.Market, uint256, uint256, uint256, address, address, bytes) external => NONDET;
    function _.isRatified(Midnight.Offer offer, bytes) external => CVL_isRatified(offer) expect(bytes32);
    function _.onFlashLoan(address, address[], uint256[], bytes) external => NONDET;
    function SafeTransferLib.safeTransferFrom(address, address, address, uint256) internal => NONDET;
    function SafeTransferLib.safeTransfer(address, address, uint256) internal => NONDET;
```

**File:** SECURITY.md (L26-26)
```markdown
Note: This does not exclude oracle manipulation/flash-loan attacks.
```
