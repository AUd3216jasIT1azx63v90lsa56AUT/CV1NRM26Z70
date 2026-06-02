The code at the referenced lines has been verified and matches the claim exactly.

**Verified code behavior:**

- [1](#0-0)  — `pullToken` pulls `assets` from `msg.sender`, then `repay` is called with `units = assets - referralFeeAssets`.
- [2](#0-1)  — `pullToken` uses `safeTransferFrom`, which succeeds but delivers only `assets*(1-fee_rate)` to MidnightBundles with fee-on-transfer tokens.
- [3](#0-2)  — `payer = msg.sender` (MidnightBundles) when `callback == address(0)`, and `safeTransferFrom` pulls `units` from MidnightBundles.
- No token safety exclusions for fee-on-transfer tokens found in `SECURITY.md` or `RESEARCHER.md`. [4](#0-3) 
- The `MidnightBundles.sol` header notes it "Inherits the token safety requirements of Midnight" but no explicit fee-on-transfer exclusion exists in `Midnight.sol`. [5](#0-4) 

---

Audit Report

## Title
Fee-on-transfer loanToken causes `repayAndWithdrawCollateral` to always revert - (`src/periphery/MidnightBundles.sol`)

## Summary
`MidnightBundles.repayAndWithdrawCollateral` pulls `assets` from `msg.sender` via `pullToken`, then calls `Midnight.repay(market, units, ...)` where `units = assets - referralFeeAssets`. With a fee-on-transfer `loanToken`, `pullToken` delivers only `assets*(1-fee_rate)` to MidnightBundles, but `Midnight.repay` attempts to pull the full `units` from MidnightBundles via `safeTransferFrom`. When `referralFeePct = 0` or when `fee_rate > referralFeePct/WAD`, MidnightBundles holds fewer tokens than `units`, causing `safeTransferFrom` inside `repay` to revert unconditionally. This permanently DoS-es the bundler's repay-and-withdraw path for any market whose `loanToken` is fee-on-transfer.

## Finding Description
**Code path:**

1. `MidnightBundles.repayAndWithdrawCollateral` (`src/periphery/MidnightBundles.sol`, lines 329–334):
   ```solidity
   uint256 referralFeeAssets = assets.mulDivDown(referralFeePct, WAD);  // line 329
   uint256 units = assets - referralFeeAssets;                           // line 330
   pullToken(loanToken, msg.sender, assets, loanTokenPermit);            // line 331
   forceApproveMax(loanToken, MIDNIGHT);                                 // line 332
   IMidnight(MIDNIGHT).repay(market, units, onBehalf, address(0), "");  // line 334
   ```

2. `pullToken` (`src/periphery/MidnightBundles.sol`, line 396) calls `SafeTransferLib.safeTransferFrom(token, from, address(this), amount)`. The call succeeds (no revert), but a fee-on-transfer token silently delivers only `assets*(1-fee_rate)` to MidnightBundles.

3. `Midnight.repay` (`src/Midnight.sol`, line 511): `payer = callback != address(0) ? callback : msg.sender`. Since `callback == address(0)`, `payer = msg.sender = MidnightBundles`.

4. `Midnight.repay` (`src/Midnight.sol`, line 520): `SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units)` — attempts to pull `units` from MidnightBundles. MidnightBundles holds only `assets*(1-fee_rate)` < `units`, so this reverts.

**Root cause:** No balance-delta accounting after `pullToken`. The function assumes `pullToken(assets)` delivers exactly `assets` to MidnightBundles, which is false for fee-on-transfer tokens.

**Why existing checks fail:** The only guards are `require(referralFeePct < WAD)` and the authorization check (`src/periphery/MidnightBundles.sol`, lines 325–326). Neither validates the actual received balance against `units`.

## Impact Explanation
`repayAndWithdrawCollateral` is permanently and deterministically broken for any market whose `loanToken` is fee-on-transfer. Every call reverts at the `safeTransferFrom` inside `Midnight.repay`. Borrowers cannot use the bundler to atomically repay debt and withdraw collateral. No funds are permanently lost since direct `Midnight.repay` remains available, but the bundler's atomic repay-and-withdraw path is fully DoS-ed for this token class. This constitutes service unavailability for a deployed, in-scope contract function.

## Likelihood Explanation
**Preconditions:**
- Market's `loanToken` is fee-on-transfer (e.g., USDT with fee enabled, STA, PAXG, or any custom token)
- Borrower has outstanding debt in that market
- Borrower calls `repayAndWithdrawCollateral` via MidnightBundles

**Feasibility:** Moderate. Market creation in Midnight is permissionless, so any user can create a market with a fee-on-transfer loanToken. Fee-on-transfer tokens exist in production. The bug is deterministic — it triggers on every call, not probabilistically. No special permissions are required beyond having debt and having approved MidnightBundles.

**Repeatability:** 100% — every call to `repayAndWithdrawCollateral` with a fee-on-transfer loanToken and `referralFeePct = 0` (or `fee_rate > referralFeePct/WAD`) reverts.

## Recommendation
Replace the fixed-amount assumption with balance-delta accounting. After `pullToken`, measure the actual received balance and use that as the basis for `units`:

```solidity
uint256 balanceBefore = IERC20(loanToken).balanceOf(address(this));
pullToken(loanToken, msg.sender, assets, loanTokenPermit);
uint256 received = IERC20(loanToken).balanceOf(address(this)) - balanceBefore;
uint256 referralFeeAssets = received.mulDivDown(referralFeePct, WAD);
uint256 units = received - referralFeeAssets;
forceApproveMax(loanToken, MIDNIGHT);
IMidnight(MIDNIGHT).repay(market, units, onBehalf, address(0), "");
```

This ensures `units` never exceeds the actual balance held by MidnightBundles, regardless of token transfer fees.

## Proof of Concept
**Minimal fork test plan:**
1. Deploy a mock ERC20 with a 1% transfer fee.
2. Create a Midnight market with this token as `loanToken`.
3. Have a borrower supply collateral, borrow, and authorize MidnightBundles.
4. Call `repayAndWithdrawCollateral` with `referralFeePct = 0` and `assets = borrower's debt`.
5. Observe revert at `SafeTransferLib.safeTransferFrom` inside `Midnight.repay` because MidnightBundles holds `assets * 0.99` but attempts to transfer `assets`.
6. Confirm direct `Midnight.repay` call succeeds, isolating the bug to the bundler path.

### Citations

**File:** src/periphery/MidnightBundles.sol (L23-23)
```text
/// @dev Inherits the token safety requirements of Midnight (see Midnight.sol).
```

**File:** src/periphery/MidnightBundles.sol (L329-334)
```text
        uint256 referralFeeAssets = assets.mulDivDown(referralFeePct, WAD);
        uint256 units = assets - referralFeeAssets;
        pullToken(loanToken, msg.sender, assets, loanTokenPermit);
        forceApproveMax(loanToken, MIDNIGHT);

        IMidnight(MIDNIGHT).repay(market, units, onBehalf, address(0), "");
```

**File:** src/periphery/MidnightBundles.sol (L378-397)
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
```

**File:** src/Midnight.sol (L511-520)
```text
        address payer = callback != address(0) ? callback : msg.sender;
        emit EventsLib.Repay(msg.sender, id, units, onBehalf, payer);

        if (callback != address(0)) {
            require(
                IRepayCallback(callback).onRepay(id, market, units, onBehalf, data) == CALLBACK_SUCCESS,
                WrongRepayCallbackReturnValue()
            );
        }
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
```

**File:** SECURITY.md (L1-55)
```markdown
# Common Vulnerability Exclusion List

## Out of Scope & Rules

These are the default impacts recommended to projects to mark as out of scope for their bug bounty program. The actual list of out-of-scope impacts differs from program to program.

### General

- Impacts requiring attacks that the reporter has already exploited themselves, leading to damage.
- Impacts caused by attacks requiring access to leaked keys/credentials.
- Impacts caused by attacks requiring access to privileged addresses (governance, strategist), except in cases where the contracts are intended to have no privileged access to functions that make the attack possible.
- Impacts relying on attacks involving the depegging of an external stablecoin where the attacker does not directly cause the depegging due to a bug in code.
- Mentions of secrets, access tokens, API keys, private keys, etc. in GitHub will be considered out of scope without proof that they are in use in production.
- Best practice recommendations.
- Feature requests.
- Impacts on test files and configuration files, unless stated otherwise in the bug bounty program.

### Smart Contracts / Blockchain DLT

- Incorrect data supplied by third-party oracles.
- Impacts requiring basic economic and governance attacks (e.g. 51% attack).
- Lack of liquidity impacts.
- Impacts from Sybil attacks.
- Impacts involving centralization risks.

Note: This does not exclude oracle manipulation/flash-loan attacks.

### Websites and Apps

- Theoretical impacts without any proof or demonstration.
- Impacts involving attacks requiring physical access to the victim device.
- Impacts involving attacks requiring access to the local network of the victim.
- Reflected plain text injection (e.g. URL parameters, path, etc.).
- This does not exclude reflected HTML injection with or without JavaScript.
- This does not exclude persistent plain text injection.
- Any impacts involving self-XSS.
- Captcha bypass using OCR without impact demonstration.
- CSRF with no state-modifying security impact (e.g. logout CSRF).
- Impacts related to missing HTTP security headers (such as `X-FRAME-OPTIONS`) or cookie security flags (such as `httponly`) without demonstration of impact.
- Server-side non-confidential information disclosure, such as IPs, server names, and most stack traces.
- Impacts causing only the enumeration or confirmation of the existence of users or tenants.
- Impacts caused by vulnerabilities requiring unprompted, in-app user actions that are not part of the normal app workflows.
- Lack of SSL/TLS best practices.
- Impacts that only require DDoS.
- UX and UI impacts that do not materially disrupt use of the platform.
- Impacts primarily caused by browser/plugin defects.
- Leakage of non-sensitive API keys (e.g. Etherscan, Infura, Alchemy, etc.).
- Any vulnerability exploit requiring browser bugs for exploitation (e.g. CSP bypass).
- SPF/DMARC misconfigured records.
- Missing HTTP headers without demonstrated impact.
- Automated scanner reports without demonstrated impact.
- UI/UX best practice recommendations.
- Non-future-proof NFT rendering.

## Prohibited Activities
```
