### Title
Hardcoded `PERMIT2` Address May Not Be Deployed on New Networks, Breaking Permit2 Token Pulls — (File: src/periphery/MidnightBundles.sol)

### Summary
`MidnightBundles` hardcodes the Permit2 contract address as a `constant` at `0x000000000022D473030F116dDEE9F6B43aC78BA3`. On networks where Permit2 has not been deployed at that address, calling `pullToken` with `PermitKind.Permit2` silently performs no token transfer (a call to an empty address returns `(true, "")` in Solidity), causing all downstream bundler operations to revert or, in edge cases where the bundler holds a residual balance, to drain it without the caller paying.

### Finding Description

`MidnightBundles` declares:

```solidity
address public constant PERMIT2 = 0x000000000022D473030F116dDEE9F6B43aC78BA3;
``` [1](#0-0) 

This constant is consumed inside `pullToken`:

```solidity
} else if (permit.kind == PermitKind.Permit2) {
    (uint256 nonce, uint256 deadline, bytes memory signature) =
        abi.decode(permit.data, (uint256, uint256, bytes));
    IPermit2(PERMIT2)
        .permitTransferFrom(
            IPermit2.PermitTransferFrom(IPermit2.TokenPermissions(token, amount), nonce, deadline),
            IPermit2.SignatureTransferDetails(address(this), amount),
            from,
            signature
        );
``` [2](#0-1) 

`pullToken` is called by every public bundle entry-point (`buyWithUnitsTargetAndWithdrawCollateral`, `buyWithAssetsTargetAndWithdrawCollateral`, `repayAndWithdrawCollateral`, and `supplyCollateralAndSellWithUnitsTarget` / `supplyCollateralAndSellWithAssetsTarget` for collateral permits). [3](#0-2) 

**Root cause chain:**

1. On a network where Permit2 is not deployed, `PERMIT2` points to an EOA/empty address.
2. Solidity's high-level interface call to `IPermit2(PERMIT2).permitTransferFrom(...)` — which has **no return value** — succeeds silently (EVM returns `(true, "")` for calls to empty addresses; no ABI-decode failure occurs because there is nothing to decode).
3. No tokens are actually transferred from `from` to the bundler.
4. The bundler then calls `forceApproveMax(loanToken, MIDNIGHT)` and proceeds to execute takes/repays, expecting to hold `amount` tokens it never received. [4](#0-3) 

### Impact Explanation

**Primary impact — DoS of all Permit2 flows:** Every bundle function that receives a `PermitKind.Permit2` permit will silently skip the token pull and then revert when Midnight attempts `safeTransferFrom` from the bundler (which holds zero tokens). All Permit2-based lending, borrowing, and repayment operations are permanently broken on the affected network.

**Secondary impact — residual balance drain:** If the bundler holds any residual token balance (e.g., from a failed prior transaction or a direct transfer), the silent no-op allows a caller to consume that balance without paying, effectively stealing tokens held by the contract. [5](#0-4) 

### Likelihood Explanation

Permit2 is deployed via CREATE2 on all major EVM-compatible networks (Ethereum, Arbitrum, Optimism, Base, Polygon, etc.), but it is **not** automatically present on new L2s, app-chains, or testnets. The Morpho Midnight protocol is designed to be deployable on any EVM network. Any deployment on a network that has not independently deployed Permit2 at the canonical address triggers this issue for every user who selects the Permit2 permit path. No privileged access is required; any unprivileged user can trigger the silent failure simply by passing `PermitKind.Permit2` in a bundle call.

### Recommendation

Replace the hardcoded constant with a constructor-injected immutable, and add a deployment-time check that the address contains code:

```solidity
address public immutable PERMIT2;

constructor(address _midnight, address _permit2) {
    MIDNIGHT = _midnight;
    require(_permit2.code.length > 0, "Permit2 not deployed");
    PERMIT2 = _permit2;
}
```

Alternatively, guard the Permit2 branch at runtime:

```solidity
} else if (permit.kind == PermitKind.Permit2) {
    require(PERMIT2.code.length > 0, Permit2NotDeployed());
    ...
}
```

### Proof of Concept

1. Deploy `MidnightBundles` on a network where `0x000000000022D473030F116dDEE9F6B43aC78BA3` has no code.
2. Call `buyWithUnitsTargetAndWithdrawCollateral` with a `loanTokenPermit` whose `kind == PermitKind.Permit2`.
3. Observe: `pullToken` executes the `IPermit2(PERMIT2).permitTransferFrom(...)` call; the EVM returns `(true, "")` (empty address, no revert, no transfer).
4. The bundler holds 0 loan tokens. `forceApproveMax` succeeds (no-op approval).
5. Midnight's `take` attempts `safeTransferFrom(loanToken, bundler, midnight, buyerAssets)` — reverts with insufficient balance.
6. Result: the entire bundle reverts; Permit2-based flows are permanently unusable on this network. [6](#0-5) [7](#0-6)

### Citations

**File:** src/periphery/MidnightBundles.sol (L30-35)
```text
    address public constant PERMIT2 = 0x000000000022D473030F116dDEE9F6B43aC78BA3;
    address public immutable MIDNIGHT;

    constructor(address _midnight) {
        MIDNIGHT = _midnight;
    }
```

**File:** src/periphery/MidnightBundles.sol (L66-104)
```text
        pullToken(loanToken, msg.sender, maxBuyerAssets, loanTokenPermit);
        forceApproveMax(loanToken, MIDNIGHT);

        uint256 filledUnits;
        uint256 filledBuyerAssets;
        for (uint256 i; i < takes.length && filledUnits < targetUnits; i++) {
            require(!takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
            uint256 unitsToTake = min(
                targetUnits - filledUnits,
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
            try IMidnight(MIDNIGHT)
                .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (
                uint256 resBuyerAssets, uint256
            ) {
                filledUnits += unitsToTake;
                filledBuyerAssets += resBuyerAssets;
            } catch {}
        }

        require(filledUnits == targetUnits, OutOfOffers());

        Market memory market = takes[0].offer.market;
        for (uint256 i; i < collateralWithdrawals.length; i++) {
            IMidnight(MIDNIGHT)
                .withdrawCollateral(
                    market,
                    collateralWithdrawals[i].collateralIndex,
                    collateralWithdrawals[i].assets,
                    taker,
                    collateralReceiver
                );
        }

        uint256 referralFeeAssets = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
        if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
        SafeTransferLib.safeTransfer(loanToken, msg.sender, maxBuyerAssets - filledBuyerAssets - referralFeeAssets);
```

**File:** src/periphery/MidnightBundles.sol (L378-398)
```text
    function pullToken(address token, address from, uint256 amount, TokenPermit memory permit) internal {
        if (permit.kind == PermitKind.ERC2612) {
            (uint256 deadline, uint8 v, bytes32 r, bytes32 s) =
                abi.decode(permit.data, (uint256, uint8, bytes32, bytes32));
            // Tolerate revert: a third party may have already consumed the permit.
            try IERC20Permit(token).permit(from, address(this), amount, deadline, v, r, s) {} catch {}
            SafeTransferLib.safeTransferFrom(token, from, address(this), amount);
        } else if (permit.kind == PermitKind.Permit2) {
            (uint256 nonce, uint256 deadline, bytes memory signature) =
                abi.decode(permit.data, (uint256, uint256, bytes));
            IPermit2(PERMIT2)
                .permitTransferFrom(
                    IPermit2.PermitTransferFrom(IPermit2.TokenPermissions(token, amount), nonce, deadline),
                    IPermit2.SignatureTransferDetails(address(this), amount),
                    from,
                    signature
                );
        } else {
            SafeTransferLib.safeTransferFrom(token, from, address(this), amount);
        }
    }
```
