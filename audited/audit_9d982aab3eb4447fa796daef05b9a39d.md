Based on my review of the actual code in `src/Midnight.sol`, `src/interfaces/ICallbacks.sol`, and `certora/specs/OnlyExplicitPayerCanLoseTokens.spec`, all code references in the claim are accurate and the exploit path is valid.

**Verification of each claim step:**

1. **Line 679** — `address payer = callback != address(0) ? callback : msg.sender;` — confirmed, no authorization check on `callback`. [1](#0-0) 

2. **`repay` line 505** — `require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized())` — confirmed. In `repay`, the caller must be authorized to act on behalf of `onBehalf` before the callback is used as payer. In `liquidate`, no equivalent gate exists. [2](#0-1) 

3. **Collateral transferred before callback** — line 696 sends seized collateral to `receiver` before `onLiquidate` is called. [3](#0-2) 

4. **`onLiquidate` called with `msg.sender` as `caller`** — lines 698–714 call `ILiquidateCallback(callback).onLiquidate(msg.sender, ...)`. The interface exposes `caller` but imposes no validation requirement. [4](#0-3) 

5. **Loan token pulled from `payer = callback`** — line 717. [5](#0-4) 

6. **`liquidatorGate` only checks `msg.sender`** — lines 597–600 verify the liquidator is permitted; says nothing about the callback/payer. [6](#0-5) 

7. **Certora spec gap** — `otherEntryPointsOnlyPullFromCaller` only asserts the pull comes from the callback that returned `CALLBACK_SUCCESS`, not that the callback consented to being the payer for this particular liquidator. `callbackAllowed` is set to `true` whenever any callback returns `CALLBACK_SUCCESS`, regardless of whether it validated `caller`. [7](#0-6) 

The broken symmetry with `repay` is the key: in `repay`, the caller must be authorized to act on behalf of `onBehalf` (the beneficiary), so the callback is always set by a trusted party. In `liquidate`, any caller (subject only to `liquidatorGate`) can designate any address as `callback`/payer with no compensating authorization check.

---

Audit Report

## Title
Liquidator-controlled `callback` parameter designates arbitrary third-party as payer with no authorization check - (File: `src/Midnight.sol`)

## Summary
In `Midnight.liquidate`, the `callback` parameter is freely supplied by the liquidator and unconditionally assigned as `payer` at line 679 with no check that the callback address has authorized the liquidator to act on its behalf. An attacker can designate any contract implementing `ILiquidateCallback` that holds a `loanToken` approval to Midnight and does not validate the `caller` argument as the payer, receiving seized collateral at an attacker-controlled `receiver` while the third-party contract bears the full repayment cost. This breaks the authorization invariant enforced by every other payer-designating function in the protocol.

## Finding Description

**Root cause:** `liquidate` allows the liquidator to set `callback` to any address, which becomes `payer` for the `safeTransferFrom` pull, with no authorization gate between the liquidator and the designated payer.

**Step 1 — payer assigned from liquidator-controlled input, no authorization check** (`src/Midnight.sol` line 679):
```solidity
address payer = callback != address(0) ? callback : msg.sender;
```
There is no `require(callback == msg.sender || isAuthorized[callback][msg.sender])` or equivalent. Compare with `repay` (line 505), which gates `onBehalf` behind `isAuthorized` before the callback is used as payer — meaning in `repay`, the callback is always set by a trusted, authorized party. In `liquidate`, the caller need only pass the `liquidatorGate` check (or the gate is `address(0)`), and can then designate any third-party address as `callback`/payer.

**Step 2 — collateral transferred to attacker-controlled `receiver` before callback fires** (line 696):
```solidity
SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
```
At this point the attacker already holds the seized collateral.

**Step 3 — `onLiquidate` called on `thirdParty`** (lines 698–714). The liquidator's address is passed as `caller`, but the protocol never enforces that the callback validates it. The `ILiquidateCallback` interface exposes `caller` as an argument but imposes no validation requirement.

**Step 4 — loan token pulled from `payer = thirdParty`** (line 717):
```solidity
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
```

**Why existing checks do not stop it:**
- The `liquidatorGate` check (lines 597–600) only verifies that `msg.sender` is permitted to liquidate; it says nothing about who the callback/payer is.
- The Certora property `otherEntryPointsOnlyPullFromCaller` (lines 119–136 of `OnlyExplicitPayerCanLoseTokens.spec`) only asserts that the pull comes from the callback that returned `CALLBACK_SUCCESS` — it does not assert that the callback consented to being the payer for this particular liquidator. The `callbackAllowed` ghost is set to `true` whenever any callback returns `CALLBACK_SUCCESS`, regardless of whether the callback validated `caller`.

## Impact Explanation
An unprivileged liquidator profits from collateral seizure (`seizedAssets` sent to `receiver = attacker`) while a third-party contract bears the entire repayment cost (`repaidUnits` pulled from `thirdParty`). The third party suffers a direct, unrecoverable loss of `loanToken` proportional to the liquidated debt. This violates the invariant that liquidation repayment must come from the liquidator or a party the liquidator is explicitly authorized to act for — an invariant enforced by every other payer-designating function (`repay`, `setConsumed`, `withdrawCollateral`, `supplyCollateral`).

## Likelihood Explanation
**Required preconditions:**
1. An unhealthy borrower position exists (normal market condition).
2. The attacker passes any `liquidatorGate` check, or the market has none (`address(0)`).
3. A `thirdParty` contract exists that: (a) implements `ILiquidateCallback` and returns `CALLBACK_SUCCESS` without validating the `caller` argument, and (b) has granted Midnight a `loanToken` allowance ≥ `repaidUnits`.

Condition 3 is realistic for flash-liquidation bots, vault integrations, and keeper contracts that pre-approve Midnight and implement the callback interface. Such contracts may omit a caller guard because the protocol exposes `caller` as an argument but never enforces its validation, creating a false expectation that the protocol itself enforces authorization — especially given that `repay` does enforce `isAuthorized` for its analogous `onBehalf` parameter. The attack is repeatable as long as the third party's allowance and balance remain sufficient.

## Recommendation
Add an authorization check before using `callback` as `payer` in `liquidate`:
```solidity
require(
    callback == address(0) || callback == msg.sender || isAuthorized[callback][msg.sender],
    Unauthorized()
);
```
This mirrors the pattern used in `repay`, `withdrawCollateral`, `supplyCollateral`, and `setConsumed`, and ensures the callback/payer has explicitly authorized the liquidator to act on its behalf. Alternatively, document clearly in the `ILiquidateCallback` interface that implementations MUST validate `caller == msg.sender` (i.e., that the call originates from the contract itself), but the on-chain check is strongly preferred as it cannot be bypassed by implementation oversight.

## Proof of Concept
1. Deploy `VulnerableBot` implementing `ILiquidateCallback` with `onLiquidate` that returns `CALLBACK_SUCCESS` unconditionally (no `caller` check). Pre-approve Midnight for a large `loanToken` amount and fund with loan tokens.
2. Create a market with `liquidatorGate = address(0)`. Set up a borrower with an unhealthy position.
3. As attacker, call:
   ```solidity
   midnight.liquidate(
       market, collateralIndex, seizedAssets, 0,
       borrower, false,
       attackerAddress,   // receiver: attacker gets collateral
       address(vulnerableBot), // callback: VulnerableBot pays
       ""
   );
   ```
4. Observe: attacker receives `seizedAssets` collateral; `VulnerableBot` loses `repaidUnits` loan tokens to Midnight with no benefit.

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

**File:** src/Midnight.sol (L696-696)
```text
        SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);
```

**File:** src/Midnight.sol (L717-717)
```text
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
```

**File:** src/interfaces/ICallbacks.sol (L16-18)
```text
interface ILiquidateCallback {
    function onLiquidate(address caller, bytes32 id, Market memory market, uint256 collateralIndex, uint256 seizedAssets, uint256 repaidUnits, address borrower, address receiver, bytes memory data, uint256 badDebt) external returns (bytes32);
}
```

**File:** certora/specs/OnlyExplicitPayerCanLoseTokens.spec (L57-66)
```text
function onCallBackSummary(address callbackAddress, bool allowedCallback) returns (bytes32) {
    assert allowedCallback;
    bytes32 result;
    triggerHavocAll();
    callback = callbackAddress;
    if (result == Utils.callbackSuccess()) {
        assert callbackAllowed == false;
        callbackAllowed = true;
    }
    return result;
```
