Audit Report

## Title
Fee-on-Transfer Collateral Token Causes Unconditional Revert in `supplyCollateralAndSellWithUnitsTarget` and `supplyCollateralAndSellWithAssetsTarget` - (File: src/periphery/MidnightBundles.sol)

## Summary
In `supplyCollateralAndSellWithUnitsTarget` and `supplyCollateralAndSellWithAssetsTarget`, the bundler pulls `collateralSupplies[i].assets` from the caller but receives only `assets*(1-f)` when the collateral token charges a transfer fee. The bundler then forwards the original nominal `assets` value to `Midnight.supplyCollateral`, which attempts to `safeTransferFrom` the full `assets` amount from the bundler. Because the bundler's balance is `assets*(1-f) < assets`, the ERC-20 transfer reverts, causing the entire transaction to revert atomically. No funds are lost (the revert is atomic), but the bundler is permanently unusable for any market whose collateral token charges a transfer fee.

## Finding Description

**Exact code path — `supplyCollateralAndSellWithUnitsTarget`:**

Step 1 — `MidnightBundles.sol` lines 135–139:
```solidity
address token = market.collateralParams[collateralSupplies[i].collateralIndex].token;
pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
forceApproveMax(token, MIDNIGHT);
IMidnight(MIDNIGHT)
    .supplyCollateral(market, collateralSupplies[i].collateralIndex, collateralSupplies[i].assets, taker);
``` [1](#0-0) 

`pullToken` (line 396) calls `SafeTransferLib.safeTransferFrom(token, from, address(this), amount)`. For a fee-on-transfer token with fee rate `f`, the bundler's balance increases by only `amount*(1-f)`. [2](#0-1) 

Step 2 — `Midnight.sol` line 545:
```solidity
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
``` [3](#0-2) 

Here `msg.sender` is the bundler and `assets` is the original nominal value. The bundler holds `assets*(1-f)` but the call requests `assets`. The ERC-20 `transferFrom` reverts with insufficient balance.

The identical pattern exists in `supplyCollateralAndSellWithAssetsTarget` at lines 270–274. [4](#0-3) 

**Root cause:** The bundler uses the caller-supplied `assets` parameter both as the pull amount and as the forward amount to Midnight, with no accounting for tokens lost to transfer fees between the two steps.

**Why existing checks fail:**
- `SafeTransferLib.safeTransferFrom` (lines 24–34) only checks call success and the boolean return value; it does not verify the recipient's balance delta. [5](#0-4) 
- `forceApproveMax` (lines 371–375) grants unlimited allowance to Midnight, so approval is not the bottleneck — the bundler's actual token balance is. [6](#0-5) 
- There is no pre/post balance check in the bundler between `pullToken` and `supplyCollateral`.
- The header comment at line 23 of `MidnightBundles.sol` states "Inherits the token safety requirements of Midnight (see Midnight.sol)" but no such requirement excluding fee-on-transfer tokens is documented anywhere in `Midnight.sol`. [7](#0-6) 

## Impact Explanation
Every call to `supplyCollateralAndSellWithUnitsTarget` or `supplyCollateralAndSellWithAssetsTarget` with a fee-on-transfer collateral token reverts unconditionally. The entire transaction is atomic, so no user funds are lost. However, the bundler is completely unusable for opening leveraged positions in any market whose collateral token charges a transfer fee. Users are forced to interact with Midnight directly, bypassing the bundler's convenience and atomicity guarantees. The impact is permanent DoS of bundler functionality for an entire class of collateral tokens, with no workaround available through the bundler interface.

## Likelihood Explanation
Market creation is permissionless, so any unprivileged actor can create a market with a fee-on-transfer collateral token. Any user who then attempts to use the bundler with `collateralSupplies` referencing that token will experience a deterministic, unconditional revert on every invocation. No privileged access is required. The failure is repeatable and cannot be mitigated by adjusting call parameters.

## Recommendation
After `pullToken`, measure the bundler's actual received balance and use that value (rather than the caller-supplied `assets`) when calling `supplyCollateral`. For example:

```solidity
uint256 balanceBefore = IERC20(token).balanceOf(address(this));
pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
uint256 received = IERC20(token).balanceOf(address(this)) - balanceBefore;
forceApproveMax(token, MIDNIGHT);
IMidnight(MIDNIGHT).supplyCollateral(market, collateralSupplies[i].collateralIndex, received, taker);
```

Apply the same fix to `supplyCollateralAndSellWithAssetsTarget`. Alternatively, explicitly document and enforce that fee-on-transfer tokens are unsupported as collateral tokens (e.g., via a NatSpec warning and/or an on-chain check in `Midnight.createMarket`).

## Proof of Concept

1. Deploy a mock ERC-20 that deducts a 1% fee on every `transferFrom` call.
2. Create a Midnight market with this token as `collateralParams[0].token` (market creation is permissionless).
3. Authorize the bundler on Midnight for a test taker account.
4. Approve the bundler to spend the fee-on-transfer token from `msg.sender`.
5. Call `supplyCollateralAndSellWithUnitsTarget` with `collateralSupplies[0].assets = 1000`.
6. Observe: `pullToken` transfers 1000 from user; bundler receives 990 (1% fee). `Midnight.supplyCollateral` attempts `safeTransferFrom(bundler, midnight, 1000)`; bundler has only 990; ERC-20 reverts. Entire transaction reverts.
7. Confirm: repeat with any nonzero `assets` value — result is always revert.

### Citations

**File:** src/periphery/MidnightBundles.sol (L23-23)
```text
/// @dev Inherits the token safety requirements of Midnight (see Midnight.sol).
```

**File:** src/periphery/MidnightBundles.sol (L135-139)
```text
            address token = market.collateralParams[collateralSupplies[i].collateralIndex].token;
            pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
            forceApproveMax(token, MIDNIGHT);
            IMidnight(MIDNIGHT)
                .supplyCollateral(market, collateralSupplies[i].collateralIndex, collateralSupplies[i].assets, taker);
```

**File:** src/periphery/MidnightBundles.sol (L270-274)
```text
            address token = market.collateralParams[collateralSupplies[i].collateralIndex].token;
            pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
            forceApproveMax(token, MIDNIGHT);
            IMidnight(MIDNIGHT)
                .supplyCollateral(market, collateralSupplies[i].collateralIndex, collateralSupplies[i].assets, taker);
```

**File:** src/periphery/MidnightBundles.sol (L371-375)
```text
    function forceApproveMax(address token, address spender) internal {
        if (IERC20(token).allowance(address(this), spender) >= type(uint96).max / 2) return;
        safeApprove(token, spender, 0);
        safeApprove(token, spender, type(uint256).max);
    }
```

**File:** src/periphery/MidnightBundles.sol (L395-397)
```text
        } else {
            SafeTransferLib.safeTransferFrom(token, from, address(this), amount);
        }
```

**File:** src/Midnight.sol (L545-545)
```text
        SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
```

**File:** src/libraries/SafeTransferLib.sol (L24-34)
```text
    function safeTransferFrom(address token, address from, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());

        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transferFrom, (from, to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
        }
        require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferFromReturnedFalse());
    }
```
